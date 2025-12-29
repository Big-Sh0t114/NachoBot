package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// 上游 QHAI 配置（可通过环境变量覆盖）
var (
	qhaiBase = envOr("QHAI_BASE", "https://api.qhaigc.net/v1")
	qhaiKey  = envOr("QHAI_KEY", "")
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envOrInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func backoff(attempt int) time.Duration {
	return time.Duration(500*math.Pow(2, float64(attempt))) * time.Millisecond
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/chat/completions", handleChat)
	srv := &http.Server{Addr: ":11435", Handler: mux}
	log.Println("Shim listening on http://127.0.0.1:11435/v1/chat/completions")
	log.Fatal(srv.ListenAndServe())
}

func handleChat(w http.ResponseWriter, r *http.Request) {
	defer r.Body.Close()
	body, _ := io.ReadAll(r.Body)

	// 强制关闭 reasoning / thinking，并设置纯文本返回
	var reqPayload map[string]any
	if err := json.Unmarshal(body, &reqPayload); err == nil {
		reqPayload["enable_thinking"] = false
		reqPayload["enable_thoughts"] = false
		reqPayload["enable_reasoning"] = false
		reqPayload["response_format"] = map[string]any{"type": "text"}
		reqPayload["thinking_budget_token_limit"] = 100
		if params, ok := reqPayload["extra_params"].(map[string]any); ok {
			params["enable_thinking"] = false
			params["enable_thoughts"] = false
			params["enable_reasoning"] = false
			params["response_format"] = map[string]any{"type": "text"}
			params["thinking_budget_token_limit"] = 100
			reqPayload["extra_params"] = params
		}
		if nb, err := json.Marshal(reqPayload); err == nil {
			body = nb
		}
	}

	req, _ := http.NewRequest("POST", qhaiBase+"/chat/completions", bytes.NewReader(body))
	if qhaiKey != "" {
		req.Header.Set("Authorization", "Bearer "+qhaiKey)
	}
	req.Header.Set("Content-Type", "application/json")

	timeout := time.Duration(envOrInt("SHIM_TIMEOUT_SECONDS", 60)) * time.Second
	retry := envOrInt("SHIM_RETRY", 2)
	client := &http.Client{Timeout: timeout}

	var resp *http.Response
	var err error
	for attempt := 0; attempt <= retry; attempt++ {
		resp, err = client.Do(req)
		if err == nil {
			break
		}
		log.Printf("Upstream request failed (attempt %d/%d): %v", attempt+1, retry+1, err)
		if attempt < retry {
			time.Sleep(backoff(attempt))
		}
	}
	if err != nil {
		writeChoices(w, 200, map[string]any{"content": "Upstream error: " + err.Error()})
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if len(raw) == 0 {
		writeChoices(w, 200, map[string]any{"content": "Upstream returned empty response (status " + resp.Status + ")"})
		return
	}

	// 尝试按 JSON 解析
	var jr map[string]any
	if err := json.Unmarshal(raw, &jr); err != nil {
		writeChoices(w, 200, map[string]any{"content": string(raw)})
		return
	}

	// 如果已经是 OpenAI 结构（有 choices），兜底填充 content
	if vv, ok := jr["choices"].([]any); ok && len(vv) > 0 {
		if first, ok := vv[0].(map[string]any); ok {
			if msg, ok := first["message"].(map[string]any); ok {
				if c, ok := getString(msg, "content"); ok && strings.TrimSpace(c) == "" {
					msg["content"] = string(raw)
					first["message"] = msg
					vv[0] = first
					jr["choices"] = vv
				}
			}
		}
		writeJSON(w, resp.StatusCode, jr)
		return
	}

	// 常见字段提取
	if s, ok := getString(jr, "output_text"); ok && strings.TrimSpace(s) != "" {
		writeChoices(w, 200, map[string]any{"content": s})
		return
	}
	if text := extractGeminiText(jr); strings.TrimSpace(text) != "" {
		writeChoices(w, 200, map[string]any{"content": text})
		return
	}
	for _, k := range []string{"text", "message", "content"} {
		if s, ok := getString(jr, k); ok && strings.TrimSpace(s) != "" {
			writeChoices(w, 200, map[string]any{"content": s})
			return
		}
		if m, ok := jr[k].(map[string]any); ok {
			if s, ok := getString(m, "content"); ok && strings.TrimSpace(s) != "" {
				writeChoices(w, 200, map[string]any{"content": s})
				return
			}
		}
	}

	// 兜底返回原始 JSON 字符串
	writeChoices(w, 200, map[string]any{"content": string(raw)})
}

func extractGeminiText(j map[string]any) string {
	cs, ok := j["candidates"].([]any)
	if !ok {
		return ""
	}
	var b strings.Builder
	for _, c := range cs {
		cm, _ := c.(map[string]any)
		content, _ := cm["content"].(map[string]any)
		parts, _ := content["parts"].([]any)
		if len(parts) == 0 {
			parts, _ = cm["parts"].([]any)
		}
		for _, p := range parts {
			pm, _ := p.(map[string]any)
			if s, ok := getString(pm, "text"); ok {
				b.WriteString(s)
			}
		}
	}
	return b.String()
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeChoices(w http.ResponseWriter, code int, opt map[string]any) {
	now := time.Now().Unix()
	out := map[string]any{
		"id":      "shim-" + time.Now().Format("150405"),
		"object":  "chat.completion",
		"created": now,
		"model":   "gemini-2.5-pro",
		"choices": []any{
			map[string]any{
				"index":         0,
				"finish_reason": "stop",
				"message": map[string]any{
					"role":    "assistant",
					"content": opt["content"],
				},
			},
		},
	}
	writeJSON(w, code, out)
}

func getString(m map[string]any, k string) (string, bool) {
	if v, ok := m[k]; ok {
		if s, ok2 := v.(string); ok2 {
			return s, true
		}
	}
	return "", false
}
