#!/usr/bin/env python3
import onnx_asr
import onnxruntime as ort

print("Available providers:", ort.get_available_providers())

model = onnx_asr.load_model(
    "nemo-parakeet-tdt-0.6b-v3",
    quantization="int8",
    providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
).with_timestamps()

print("Model type:", type(model))
print("Model dir:", dir(model))
if hasattr(model, "session"):
    print("Has session attr")
    session = model.session
    print("Session type:", type(session))
    print("Session providers:", session.get_providers())
    print("Session provider options:", session.get_provider_options())
else:
    print("No session attr")
    # look for any attribute containing session
    for attr in dir(model):
        if "session" in attr.lower():
            print(f"Found attr: {attr}")
            val = getattr(model, attr)
            print(f"  type: {type(val)}")
            if hasattr(val, "get_providers"):
                print(f"  providers: {val.get_providers()}")
