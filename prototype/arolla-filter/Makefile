# Build the Arolla Envoy Wasm filter.
#
# Outputs:
#   target/wasm32-wasip1/release/arolla_filter.wasm
#
# Common targets:
#   make build          — compile to wasm32-wasip1, release profile
#   make install-target — install the rust target (one-time)
#   make size           — print the size of the built .wasm
#   make clean          — cargo clean

WASM_TARGET := wasm32-wasip1
OUT         := target/$(WASM_TARGET)/release/arolla_filter.wasm

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
