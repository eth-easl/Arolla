# Build Stage
FROM golang:1.22 AS builder
WORKDIR /app

# Copy source code directories
COPY service_a ./service_a
COPY service_b ./service_b
COPY load_generator ./load_generator

# Initialize module
RUN go mod init perf_exp

# Build static binaries
RUN CGO_ENABLED=0 GOOS=linux go build -o service-a ./service_a/main.go
RUN CGO_ENABLED=0 GOOS=linux go build -o service-b ./service_b/main.go
RUN CGO_ENABLED=0 GOOS=linux go build -o load-generator ./load_generator/main.go

# Run Stage
FROM alpine:latest
WORKDIR /app

# Copy binaries from builder
COPY --from=builder /app/service-a .
COPY --from=builder /app/service-b .
COPY --from=builder /app/load-generator .

# Expose port (Service A/B)
EXPOSE 80

# Default command (overridden by k8s)
CMD ["./service-a"]
