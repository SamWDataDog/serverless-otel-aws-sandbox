package main

// Minimal external Lambda extension, written in Go (not bash) after
// discovering this Lambda runtime's base image has no `curl` available --
// registers with the Extensions API, launches the custom OTel Collector
// (built with the Datadog exporter via ocb, since the official OTel
// Lambda collector layer doesn't bundle it) as a background process, and
// loops on /event/next so Lambda keeps the execution environment alive.
// See FINDINGS.md "Round 7".

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"
)

type registerResponse struct {
	FunctionName string `json:"functionName"`
}

type nextEventResponse struct {
	EventType string `json:"eventType"`
}

func main() {
	runtimeAPI := os.Getenv("AWS_LAMBDA_RUNTIME_API")
	base := "http://" + runtimeAPI + "/2020-01-01/extension"

	extensionID := register(base)
	fmt.Println("[otelcol-dd-extension] registered, id=" + extensionID)

	cmd := exec.Command("/opt/otelcol/otelcol-dd-lambda", "--config=/opt/otelcol/config.yaml")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		fmt.Println("[otelcol-dd-extension] failed to start collector: " + err.Error())
	} else {
		fmt.Printf("[otelcol-dd-extension] collector started, pid=%d\n", cmd.Process.Pid)
	}

	waitForPort("127.0.0.1:4318", 10*time.Second)

	for {
		eventType := nextEvent(base, extensionID)
		fmt.Println("[otelcol-dd-extension] event: " + eventType)
		if eventType == "SHUTDOWN" {
			fmt.Println("[otelcol-dd-extension] shutdown received, giving collector 2s to flush")
			time.Sleep(2 * time.Second)
			if cmd.Process != nil {
				cmd.Process.Kill()
			}
			os.Exit(0)
		}
	}
}

func waitForPort(addr string, timeout time.Duration) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			fmt.Println("[otelcol-dd-extension] collector OTLP receiver is up on " + addr)
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	fmt.Println("[otelcol-dd-extension] WARNING: collector OTLP receiver never came up on " + addr + " within timeout")
}

func register(base string) string {
	body := strings.NewReader(`{"events": ["INVOKE", "SHUTDOWN"]}`)
	req, _ := http.NewRequest("POST", base+"/register", body)
	req.Header.Set("Lambda-Extension-Name", "otelcol-dd-extension")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Println("[otelcol-dd-extension] register error: " + err.Error())
		os.Exit(1)
	}
	defer resp.Body.Close()
	var r registerResponse
	json.NewDecoder(resp.Body).Decode(&r)
	return resp.Header.Get("Lambda-Extension-Identifier")
}

func nextEvent(base, extensionID string) string {
	req, _ := http.NewRequest("GET", base+"/event/next", nil)
	req.Header.Set("Lambda-Extension-Identifier", extensionID)
	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Println("[otelcol-dd-extension] event/next error: " + err.Error())
		time.Sleep(1 * time.Second)
		return ""
	}
	defer resp.Body.Close()
	var e nextEventResponse
	json.NewDecoder(resp.Body).Decode(&e)
	return e.EventType
}
