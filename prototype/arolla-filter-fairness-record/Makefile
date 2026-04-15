# Build the Arolla fairness Envoy Wasm filter (with per-tenant metrics).
#
# Outputs:
#   target/wasm32-wasip1/release/arolla_filter_fairness_record.wasm

WASM_TARGET := wasm32-wasip1
OUT         := target/$(WASM_TARGET)/release/arolla_filter_fairness_record.wasm

.PHONY: build install-target size clean

build:
	cargo build --target $(WASM_TARGET) --release
	@echo
	@echo "Built: $(OUT)"
	@ls -lh $(OUT) 2>/dev/null || true

install-target:
	rustup target add $(WASM_TARGET)

size:
	@ls -lh $(OUT)

clean:
	cargo clean
