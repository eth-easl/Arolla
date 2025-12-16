package main

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

var client *http.Client

func main() {
	// reuse client for connection reuse
	client = &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        2000,
			MaxIdleConnsPerHost: 2000,
			IdleConnTimeout:     90 * time.Second,
		},
		Timeout: 5 * time.Second,
	}

	http.HandleFunc("/", handler)
	fmt.Printf("Starting Service A on port 80\n")
	if err := http.ListenAndServe(":80", nil); err != nil {
		fmt.Printf("Error: %v\n", err)
	}
}

func handler(w http.ResponseWriter, r *http.Request) {
	// Service A logic: call Service B
	resp, err := client.Get("http://service-b")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte(fmt.Sprintf("Service A failed: %v", err)))
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	w.WriteHeader(resp.StatusCode)
	fmt.Fprintf(w, "Service A -> %s", body)
}
