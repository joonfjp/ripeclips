#!/usr/bin/env python3
import inspect
import onnx_asr

print(
    "onnx_asr version:",
    onnx_asr.__version__ if hasattr(onnx_asr, "__version__") else "Unknown",
)

# Check load_model signature
print("\nChecking load_model function...")
try:
    sig = inspect.signature(onnx_asr.load_model)
    print("load_model signature:", sig)

    # Check parameters
    print("\nParameters:")
    for param_name, param in sig.parameters.items():
        print(f"  {param_name}: {param}")

except Exception as e:
    print(f"Error: {e}")

# Try to see if there are session options
print("\nChecking for session options parameter...")
# Let's try to call with extra kwargs
try:
    # Try to see what happens with provider_options
    import onnxruntime as ort

    print("Attempting to create model with custom session options...")

    # Check if onnx_asr has any exposed configuration
    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", quantization="int8")
    print("Model loaded successfully")

    # Check if model has any session attribute
    if hasattr(model, "session"):
        print("Model has session attribute")
        session = model.session
        print(
            f"Session options: intra_op_num_threads={session.get_session_options().intra_op_num_threads}"
        )
        print(
            f"Session options: inter_op_num_threads={session.get_session_options().inter_op_num_threads}"
        )
    else:
        print("Model doesn't have direct session access")

except Exception as e:
    print(f"Error: {e}")
    import traceback

    traceback.print_exc()
