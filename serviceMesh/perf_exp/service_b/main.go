package main

import (
	"fmt"
	"net/http"
)

func main() {
	http.HandleFunc("/", handler)
	fmt.Printf("Starting Service B on port 80\n")
	if err := http.ListenAndServe(":80", nil); err != nil {
		fmt.Printf("Error: %v\n", err)
	}
}

func handler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("Service B Success"))
}
