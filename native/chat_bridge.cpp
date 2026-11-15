// chat_bridge.cpp - a tiny, stable C API on top of llama.cpp for the Python app (loaded with ctypes).
// Same file as in QwenChat-Android; on Windows it is built into chatbridge.dll (see build_windows.ps1).
//
// Why a bridge instead of calling llama.h from Python directly: llama.cpp's public structs
// (llama_model_params, llama_context_params, batches...) change between releases, and getting a
// struct layout wrong in ctypes crashes silently. This file is compiled against the pinned llama.cpp
// tag, so the compiler checks every call, and Python only sees plain ints, floats and strings.
//
// Written against llama.cpp tag b11963 (llama_batch_ext / llama_process API).
//
// Usage from Python (one thread at a time - the app runs every call on its worker thread):
//   chat_load(path, n_ctx, n_threads, n_threads_batch, n_batch)
//                                                     -> 0 ok, <0 error (see chat_error())
//   chat_start(prompt, temp, top_p, top_k, repeat_penalty, seed)
//                                                     -> tokens evaluated (>=0), <0 error
//   chat_next(buf, buf_len)                           -> bytes of the next text piece (>0),
//                                                        0 = reply finished, <0 error
//   chat_abort()  - stops a long prompt evaluation from another thread
//   chat_reset()  - forget the conversation (clears the KV cache)
//   chat_cached_tokens() - tokens currently in the KV cache (context fill)
//   chat_free()   - unload everything
//
// The KV cache is kept between turns: chat_start() compares the new prompt with the tokens already
// in the cache and only evaluates what changed. On a phone this turns a multi-second prompt
// re-evaluation per message into evaluating just the new message.
//
// Context shift: when the app forgets the oldest messages (the chat outgrew the context), the new
// prompt is "system prompt + newer messages". Instead of re-evaluating all of it, chat_start() deletes
// the forgotten span from the KV cache and shifts the rest back - no re-evaluation at all.
//
// Memory: the KV cache (n_ctx tokens) and the compute buffers are allocated once in chat_load(), so
// the native memory use does not grow during a conversation.

#include "llama.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#ifdef __ANDROID__
#include <android/log.h>
#endif

#ifdef _WIN32
#define BRIDGE_API extern "C" __declspec(dllexport)   // chatbridge.dll exports only the chat_* functions
#else
#define BRIDGE_API extern "C" __attribute__((visibility("default")))
#endif

namespace {

llama_model *       g_model = nullptr;
llama_context *     g_ctx   = nullptr;
const llama_vocab * g_vocab = nullptr;
llama_sampler *     g_smpl  = nullptr;
llama_batch_ext *   g_batch = nullptr;

std::vector<llama_token> g_cache;                 // tokens currently stored in the KV cache (sequence 0)
llama_token              g_pending = LLAMA_TOKEN_NULL; // sampled but not yet evaluated
int                      g_n_batch = 256;
std::atomic<bool>        g_abort{false};
std::string              g_error;
std::string              g_last_log_error;

void log_callback(enum ggml_log_level level, const char * text, void * /*user_data*/) {
    if (level == GGML_LOG_LEVEL_ERROR) {
        g_last_log_error = text ? text : "";
    }
#ifdef __ANDROID__
    // Visible with: adb logcat -s llama
    int prio = ANDROID_LOG_DEBUG;
    if (level == GGML_LOG_LEVEL_ERROR) prio = ANDROID_LOG_ERROR;
    else if (level == GGML_LOG_LEVEL_WARN) prio = ANDROID_LOG_WARN;
    else if (level == GGML_LOG_LEVEL_INFO) prio = ANDROID_LOG_INFO;
    __android_log_write(prio, "llama", text ? text : "");
#else
    if (level >= GGML_LOG_LEVEL_WARN && level != GGML_LOG_LEVEL_CONT) {
        fputs(text ? text : "", stderr);
    }
#endif
}

bool abort_callback(void * /*data*/) {
    return g_abort.load();
}

int fail(const std::string & message, int code) {
    g_error = message;
    if (!g_last_log_error.empty()) {
        g_error += " (" + g_last_log_error + ")";
        g_last_log_error.clear();
    }
    return code;
}

// Appends n tokens to the KV cache (at positions g_cache.size()...) in chunks of n_batch and asks for
// the logits of the last one. Returns 0 on success, 2 if aborted, else llama_process's error code.
int evaluate(const llama_token * tokens, size_t n) {
    for (size_t start = 0; start < n; start += g_n_batch) {
        if (g_abort.load()) {
            return 2;
        }
        const size_t end = std::min(n, start + (size_t) g_n_batch);
        const size_t pos0 = g_cache.size();
        llama_batch_ext_clear(g_batch);
        for (size_t i = start; i < end; ++i) {
            const int32_t idx = llama_batch_ext_add_token(g_batch, 0, tokens[i]);
            if (idx < 0) {
                return -10;
            }
            const llama_pos pos = (llama_pos) (pos0 + (i - start));
            llama_batch_ext_set_pos(g_batch, idx, &pos);
        }
        if (end == n) {
            llama_batch_ext_set_output_logits(g_batch, (int32_t) (end - start - 1), true);
        }
        const int32_t ret = llama_process(g_ctx, LLAMA_PROCESS_TYPE_DECODE, g_batch);
        if (ret != 0) {
            return ret;
        }
        // Only now are these tokens really in the KV cache
        g_cache.insert(g_cache.end(), tokens + start, tokens + end);
    }
    return 0;
}

void free_sampler() {
    if (g_smpl) {
        llama_sampler_free(g_smpl);
        g_smpl = nullptr;
    }
}

} // namespace

BRIDGE_API const char * chat_error(void) {
    return g_error.c_str();
}

BRIDGE_API const char * chat_system_info(void) {
    // Lists the CPU features the build uses, e.g. "NEON = 1 | ARM_FMA = 1 | DOTPROD = 1 | ..."
    return llama_print_system_info();
}

BRIDGE_API void chat_free(void) {
    free_sampler();
    if (g_batch) { llama_batch_ext_free(g_batch); g_batch = nullptr; }
    if (g_ctx)   { llama_free(g_ctx);             g_ctx   = nullptr; }
    if (g_model) { llama_model_free(g_model);     g_model = nullptr; }
    g_vocab = nullptr;
    g_cache.clear();
    g_pending = LLAMA_TOKEN_NULL;
}

BRIDGE_API int chat_load(const char * path, int n_ctx, int n_threads, int n_threads_batch, int n_batch) {
    chat_free();
    g_error.clear();
    g_last_log_error.clear();

    static bool backend_ready = false;
    if (!backend_ready) {
        llama_log_set(log_callback, nullptr);
        llama_backend_init();
        backend_ready = true;
    }

    llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = 0;                  // CPU only (no GPU backend is compiled in)
    mparams.load_mode    = LLAMA_LOAD_MODE_MMAP; // weights stay file-backed: the OS can page them out
                                               // under memory pressure instead of using RAM twice
    g_model = llama_model_load_from_file(path, mparams);
    if (!g_model) {
        return fail(std::string("could not load model file: ") + path, -1);
    }
    g_vocab = llama_model_get_vocab(g_model);

    g_n_batch = n_batch > 0 ? n_batch : 256;

    llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx           = (uint32_t) n_ctx;
    cparams.n_batch         = (uint32_t) g_n_batch;
    cparams.n_ubatch        = (uint32_t) g_n_batch; // smaller ubatch = smaller compute buffer (less RAM)
    cparams.n_seq_max       = 1;
    // We only ever read the logits of the last token. The default (n_batch outputs) makes llama.cpp
    // reserve n_batch x 151,936 (Qwen vocabulary) floats in the compute buffer: ~148 MB at n_batch 256.
    cparams.n_outputs_max   = 1;
    cparams.n_threads       = n_threads;        // generating one token at a time (memory-bound)
    cparams.n_threads_batch = n_threads_batch;  // evaluating prompts (compute-bound)
    cparams.no_perf         = true;
    cparams.abort_callback      = abort_callback;
    cparams.abort_callback_data = nullptr;

    g_ctx = llama_init_from_model(g_model, cparams);
    if (!g_ctx) {
        chat_free();
        return fail("could not create the context (not enough memory? try a smaller n_ctx)", -2);
    }

    g_batch = llama_batch_ext_init(g_ctx);
    if (!g_batch) {
        chat_free();
        return fail("could not allocate the batch", -3);
    }
    return 0;
}

BRIDGE_API void chat_abort(void) {
    g_abort.store(true);
}

BRIDGE_API void chat_reset(void) {
    if (g_ctx) {
        llama_memory_clear(llama_get_memory(g_ctx), true);
    }
    g_cache.clear();
    g_pending = LLAMA_TOKEN_NULL;
}

BRIDGE_API int chat_cached_tokens(void) {
    return (int) g_cache.size();
}

BRIDGE_API int chat_count_tokens(const char * text) {
    if (!g_vocab || !text) {
        return -1;
    }
    const int32_t n = llama_tokenize(g_vocab, text, (int32_t) strlen(text), nullptr, 0, false, true);
    return n < 0 ? -n : n;
}

BRIDGE_API int chat_start(const char * prompt, float temp, float top_p, int top_k, float repeat_penalty,
                          unsigned int seed) {
    if (!g_ctx) {
        return fail("model not loaded", -1);
    }
    g_abort.store(false);
    g_error.clear();
    g_pending = LLAMA_TOKEN_NULL; // a reply that was stopped early never got its last token evaluated

    // Tokenize. add_special = true lets the model add BOS if it wants one (Qwen doesn't);
    // parse_special = true turns "<|im_start|>" etc. into their special tokens.
    const int32_t text_len = (int32_t) strlen(prompt);
    int32_t n = -llama_tokenize(g_vocab, prompt, text_len, nullptr, 0, true, true);
    std::vector<llama_token> tokens(n > 0 ? n : 0);
    if (n <= 0 || llama_tokenize(g_vocab, prompt, text_len, tokens.data(), n, true, true) < 0) {
        return fail("could not tokenize the prompt", -2);
    }

    const int n_ctx = (int) llama_n_ctx(g_ctx);
    if ((int) tokens.size() >= n_ctx - 8) {
        return fail("prompt is longer than the context", -3);
    }

    // Reuse the part of the KV cache that matches the start of the new prompt
    llama_memory_t mem = llama_get_memory(g_ctx);
    size_t keep = 0;
    while (keep < g_cache.size() && keep < tokens.size() && g_cache[keep] == tokens[keep]) {
        ++keep;
    }

    // Context shift: if old messages were dropped right after the matching prefix (the system prompt),
    // the rest of the new prompt appears further along in the cache. Find it, delete the dropped span
    // from the KV cache and shift what follows back into place.
    // (Text between special tokens is tokenized independently, so the kept messages have exactly the
    // same tokens as before.)
    const size_t probe = std::min<size_t>(64, tokens.size() - keep);
    if (probe >= 16 && keep < g_cache.size() && llama_memory_can_shift(mem)) {
        for (size_t dropped = 1; keep + dropped + probe <= g_cache.size(); ++dropped) {
            if (!std::equal(tokens.begin() + keep, tokens.begin() + keep + probe,
                            g_cache.begin() + keep + dropped)) {
                continue;
            }
            if (llama_memory_seq_rm(mem, 0, (llama_pos) keep, (llama_pos) (keep + dropped))) {
                llama_memory_seq_add(mem, 0, (llama_pos) (keep + dropped), -1, -(llama_pos) dropped);
                g_cache.erase(g_cache.begin() + keep, g_cache.begin() + keep + dropped);
                while (keep < g_cache.size() && keep < tokens.size() && g_cache[keep] == tokens[keep]) {
                    ++keep;
                }
            }
            break;
        }
    }

    if (keep == tokens.size()) {
        keep = tokens.size() - 1; // re-evaluate the last token so we get fresh logits
    }
    if (keep < g_cache.size()) {
        if (!llama_memory_seq_rm(mem, 0, (llama_pos) keep, -1)) {
            llama_memory_clear(mem, true);
            keep = 0;
        }
        g_cache.resize(keep);
    }

    const int ret = evaluate(tokens.data() + keep, tokens.size() - keep);
    if (ret == 2) {
        return fail("stopped", -4);
    }
    if (ret != 0) {
        // The cache may hold part of the prompt - safest is to start from scratch next time
        chat_reset();
        return fail("prompt evaluation failed (code " + std::to_string(ret) + ")", -5);
    }

    // Sampler chain for this reply
    free_sampler();
    llama_sampler_chain_params sparams = llama_sampler_chain_default_params();
    sparams.no_perf = true;
    g_smpl = llama_sampler_chain_init(sparams);
    if (repeat_penalty > 0.0f && repeat_penalty != 1.0f) {
        llama_sampler_chain_add(g_smpl, llama_sampler_init_penalties(
            llama_vocab_n_tokens(g_vocab), 64, repeat_penalty, 0.0f, 0.0f));
    }
    if (temp <= 0.0f) {
        llama_sampler_chain_add(g_smpl, llama_sampler_init_greedy());
    } else {
        if (top_k > 0)                   llama_sampler_chain_add(g_smpl, llama_sampler_init_top_k(top_k));
        if (top_p > 0.0f && top_p < 1.0f) llama_sampler_chain_add(g_smpl, llama_sampler_init_top_p(top_p, 1));
        llama_sampler_chain_add(g_smpl, llama_sampler_init_temp(temp));
        llama_sampler_chain_add(g_smpl, llama_sampler_init_dist(seed));
    }

    return (int) (tokens.size() - keep);
}

BRIDGE_API int chat_next(char * buf, int buf_len) {
    if (!g_ctx || !g_smpl) {
        return fail("chat_start() was not called", -1);
    }

    // A few tokens have no visible text; skip them (bounded, so a broken model can't loop forever)
    for (int attempt = 0; attempt < 16; ++attempt) {
        // Evaluate the token we returned last time, so its logits give us the next one
        if (g_pending != LLAMA_TOKEN_NULL) {
            const llama_token pending = g_pending;
            g_pending = LLAMA_TOKEN_NULL;
            if ((int) g_cache.size() >= (int) llama_n_ctx(g_ctx) - 1) {
                return fail("context is full", -2);
            }
            const int ret = evaluate(&pending, 1);
            if (ret != 0) {
                return fail("token evaluation failed (code " + std::to_string(ret) + ")", -3);
            }
        }

        const llama_token token = llama_sampler_sample(g_smpl, g_ctx, -1);
        if (llama_vocab_is_eog(g_vocab, token)) {
            return 0; // end of the reply (<|im_end|> for Qwen)
        }
        g_pending = token;

        // special = false: never render control tokens as text
        const int32_t n = llama_token_to_piece(g_vocab, token, buf, buf_len, 0, false);
        if (n < 0) {
            return fail("text piece larger than the buffer", -4);
        }
        if (n > 0) {
            return n;
        }
    }
    return 0;
}
