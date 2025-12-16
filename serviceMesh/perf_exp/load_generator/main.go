package main

import (
	"bufio"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"
)

type Result struct {
	Timestamp int64   // Unix Micro
	Latency   float64 // ms
	Status    int
}

func main() {
	if len(os.Args) < 3 {
		fmt.Println("Usage: ./load-generator <RPS> <DURATION> [URL]")
		os.Exit(1)
	}

	rps, _ := strconv.Atoi(os.Args[1])
	duration, _ := strconv.Atoi(os.Args[2])
	url := "http://service-a"
	if len(os.Args) > 3 {
		url = os.Args[3]
	}

	// Output File Setup
	outFile, err := os.Create("requests.csv")
	if err != nil {
		fmt.Printf("Error creating CSV: %v\n", err)
		os.Exit(1)
	}
	defer outFile.Close()

	writer := bufio.NewWriter(outFile)
	defer writer.Flush()

	// Write Header
	// timestamp_micro, status_code, latency_ms
	writer.WriteString("timestamp,status,latency\n")

	// Channel for results to avoid mutex contention on file writes
	resultChan := make(chan Result, rps*duration)
	var wgWriter sync.WaitGroup
	wgWriter.Add(1)

	// Writer Routine
	go func() {
		defer wgWriter.Done()
		for res := range resultChan {
			// Fast string formatting
			line := fmt.Sprintf("%d,%d,%.3f\n", res.Timestamp, res.Status, res.Latency)
			writer.WriteString(line)
		}
	}()

	// High performance client setup
	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        rps * 2,
			MaxIdleConnsPerHost: rps * 2,
			IdleConnTimeout:     90 * time.Second,
		},
		Timeout: 5 * time.Second,
	}

	fmt.Printf("Starting load: RPS=%d, Duration=%ds, URL=%s\n", rps, duration, url)

	// Pre-allocate slice for stats
	latencies := make([]float64, 0, rps*duration)
	var mu sync.Mutex
	var wg sync.WaitGroup

	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

	// Loop for Duration seconds
	for i := 0; i < duration; i++ {
		<-ticker.C
		wg.Add(rps)
		for j := 0; j < rps; j++ {
			go func() {
				defer wg.Done()
				reqStart := time.Now()
				
				resp, err := client.Get(url)
				
				// Capture latency immediately
				duration := time.Since(reqStart)
				lat := float64(duration.Microseconds()) / 1000.0 // ms
				status := 0
				
				if err == nil {
					status = resp.StatusCode
					// Read body to reuse connection
					io.Copy(io.Discard, resp.Body)
					resp.Body.Close()
				} else {
					status = 599 // Application-level error code
				}

				// Send to channel for CSV logging
				resultChan <- Result{
					Timestamp: reqStart.UnixMicro(),
					Latency:   lat,
					Status:    status,
				}

				// Store successful latencies for summary stats
				if status == 200 {
					mu.Lock()
					latencies = append(latencies, lat)
					mu.Unlock()
				}
			}()
		}
	}

	wg.Wait()
	close(resultChan) // Stop writer
	wgWriter.Wait()   // Wait for file flush

	// Calculate Stats
	sort.Float64s(latencies)
	count := len(latencies)
	
	totalRequests := rps * duration
	successRate := 0.0
	if totalRequests > 0 {
		successRate = (float64(count) / float64(totalRequests)) * 100.0
	}

	mean := 0.0
	p50 := 0.0
	p90 := 0.0
	p99 := 0.0

	if count > 0 {
		sum := 0.0
		for _, l := range latencies {
			sum += l
		}
		mean = sum / float64(count)
		p50 = latencies[int(math.Floor(float64(count)*0.50))]
		
		p90_idx := int(math.Floor(float64(count)*0.90))
		if p90_idx >= count { p90_idx = count - 1 }
		p90 = latencies[p90_idx]
		
		p99_idx := int(math.Floor(float64(count)*0.99))
		if p99_idx >= count { p99_idx = count - 1 }
		p99 = latencies[p99_idx]
	}

	fmt.Printf("--- Results for RPS=%d ---\n", rps)
	fmt.Printf("Requests: %d\n", count)
	fmt.Printf("Mean: %.2f ms\n", mean)
	fmt.Printf("P50: %.2f ms\n", p50)
	fmt.Printf("P90: %.2f ms\n", p90)
	fmt.Printf("P99: %.2f ms\n", p99)
	fmt.Printf("SuccessRate: %.2f%%\n", successRate)
	fmt.Println("----------------------------------")
}
