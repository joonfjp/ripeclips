#!/usr/bin/env python3
import os
import onnxruntime as ort

print("ONNX Runtime version:", ort.__version__)
print("\nAvailable execution providers:")
for provider in ort.get_available_providers():
    print(f"  - {provider}")

print("\nEnvironment variables:")
env_vars = [
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "ORT_NUM_THREADS",
]
for var in env_vars:
    print(f"  {var}: {os.environ.get(var, 'Not set')}")

# Try to create a session with optimized settings
print("\nCreating test session with optimized settings...")
try:
    # Set session options
    session_options = ort.SessionOptions()

    # Try to get current thread settings
    print(
        f"SessionOptions intra_op_num_threads: {session_options.intra_op_num_threads}"
    )
    print(
        f"SessionOptions inter_op_num_threads: {session_options.inter_op_num_threads}"
    )

    # Set to use all cores
    session_options.intra_op_num_threads = 0  # 0 means use all available
    session_options.inter_op_num_threads = 1  # For inference, usually 1 is fine

    print(f"\nAfter setting:")
    print(f"  intra_op_num_threads: {session_options.intra_op_num_threads}")
    print(f"  inter_op_num_threads: {session_options.inter_op_num_threads}")

except Exception as e:
    print(f"Error: {e}")

print(f"\nCPU count from os: {os.cpu_count()}")
