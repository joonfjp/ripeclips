#!/usr/bin/env python3
"""
Benchmark script for Parakeet TDT 0.6B V3 ONNX Speech Recognition Service
Measures transcription performance, speed, and resource usage
"""

import os
import sys
import time
import json
import subprocess
import statistics
import psutil
import requests
from pathlib import Path
from datetime import datetime

# Configuration
API_URL = "http://127.0.0.1:5092/v1/audio/transcriptions"
TEST_AUDIO_DIR = "/home/op/mp3"
OUTPUT_DIR = "./benchmark_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def get_audio_duration(file_path: str) -> float:
    """Get audio duration in seconds using ffprobe"""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout)
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Could not get duration of file '{file_path}': {e}")
        return 0.0


def get_file_size_mb(file_path: str) -> float:
    """Get file size in MB"""
    return os.path.getsize(file_path) / (1024 * 1024)


def transcribe_audio(
    file_path: str, model: str = "whisper-1", format: str = "text"
) -> dict:
    """Transcribe audio file and return results with timing"""
    start_time = time.time()

    with open(file_path, "rb") as audio_file:
        files = {"file": audio_file}
        data = {"model": model, "response_format": format}

        response = requests.post(API_URL, files=files, data=data)

    end_time = time.time()
    processing_time = end_time - start_time

    result = {
        "processing_time": processing_time,
        "success": response.status_code == 200,
        "status_code": response.status_code,
        "response": response.text if response.status_code == 200 else None,
        "error": response.text if response.status_code != 200 else None,
    }

    return result


def get_process_stats(pid: int) -> dict:
    """Get CPU and memory usage for a process"""
    try:
        process = psutil.Process(pid)
        cpu_percent = process.cpu_percent(interval=0.1)
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / (1024 * 1024)

        return {
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "threads": process.num_threads(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"cpu_percent": 0, "memory_mb": 0, "threads": 0}


def find_service_pid() -> int:
    """Find the PID of the parakeet-onnx service"""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if cmdline and "app.py" in " ".join(cmdline):
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return -1


def run_benchmark(audio_files: list, num_runs: int = 3):
    """Run benchmark on multiple audio files"""
    service_pid = find_service_pid()
    if service_pid == -1:
        print("Warning: Could not find service PID for resource monitoring")

    results = []
    summary_stats = {
        "total_files": len(audio_files),
        "total_runs": num_runs * len(audio_files),
        "files": {},
    }

    for audio_file in audio_files:
        file_name = os.path.basename(audio_file)
        file_size_mb = get_file_size_mb(audio_file)
        duration = get_audio_duration(audio_file)

        if duration == 0:
            print(f"Skipping {file_name}: could not get duration")
            continue

        print(f"\n{'=' * 60}")
        print(f"Benchmarking: {file_name}")
        print(f"Size: {file_size_mb:.2f} MB, Duration: {duration:.2f} seconds")
        print(f"{'=' * 60}")

        file_results = []
        processing_times = []
        real_time_factors = []

        for run in range(num_runs):
            print(f"\nRun {run + 1}/{num_runs}...")

            # Get baseline stats before transcription
            baseline_stats = get_process_stats(service_pid) if service_pid != -1 else {}

            # Transcribe
            result = transcribe_audio(audio_file, model="whisper-1", format="text")

            # Get stats after transcription
            post_stats = get_process_stats(service_pid) if service_pid != -1 else {}

            if result["success"]:
                processing_time = result["processing_time"]
                rtf = processing_time / duration  # Real Time Factor

                file_result = {
                    "run": run + 1,
                    "processing_time": processing_time,
                    "real_time_factor": rtf,
                    "speedup": duration / processing_time if processing_time > 0 else 0,
                    "baseline_stats": baseline_stats,
                    "post_stats": post_stats,
                    "text_length": len(result["response"]) if result["response"] else 0,
                }

                file_results.append(file_result)
                processing_times.append(processing_time)
                real_time_factors.append(rtf)

                print(f"  Processing time: {processing_time:.2f}s")
                print(f"  Real Time Factor: {rtf:.3f}")
                print(f"  Speedup: {duration / processing_time:.1f}x")
                print(f"  Memory: {post_stats.get('memory_mb', 0):.1f} MB")

                # Save transcription sample
                if run == 0:
                    sample_file = os.path.join(
                        OUTPUT_DIR, f"{Path(file_name).stem}_sample.txt"
                    )
                    with open(sample_file, "w", encoding="utf-8") as f:
                        f.write(
                            result["response"][:500] + "...\n"
                            if len(result["response"]) > 500
                            else result["response"]
                        )
            else:
                print(f"  Failed: {result['error']}")

        if file_results:
            avg_processing_time = statistics.mean(processing_times)
            avg_rtf = statistics.mean(real_time_factors)
            avg_speedup = statistics.mean(
                [
                    dur / processing_times[i]
                    for i, dur in enumerate([duration] * len(processing_times))
                ]
            )

            file_summary = {
                "file_name": file_name,
                "file_size_mb": file_size_mb,
                "duration": duration,
                "avg_processing_time": avg_processing_time,
                "avg_real_time_factor": avg_rtf,
                "avg_speedup": avg_speedup,
                "min_processing_time": min(processing_times),
                "max_processing_time": max(processing_times),
                "std_processing_time": statistics.stdev(processing_times)
                if len(processing_times) > 1
                else 0,
                "runs": file_results,
            }

            results.append(file_summary)

            summary_stats["files"][file_name] = {
                "duration": duration,
                "size_mb": file_size_mb,
                "avg_processing_time": avg_processing_time,
                "avg_rtf": avg_rtf,
                "avg_speedup": avg_speedup,
            }

            print(f"\nSummary for {file_name}:")
            print(f"  Average Processing Time: {avg_processing_time:.2f}s")
            print(f"  Average Real Time Factor: {avg_rtf:.3f}")
            print(f"  Average Speedup: {avg_speedup:.1f}x")
            print(
                f"  Best: {min(processing_times):.2f}s ({duration / min(processing_times):.1f}x)"
            )
            print(
                f"  Worst: {max(processing_times):.2f}s ({duration / max(processing_times):.1f}x)"
            )

    # Calculate overall statistics
    if results:
        all_processing_times = [r["avg_processing_time"] for r in results]
        all_rtfs = [r["avg_real_time_factor"] for r in results]
        all_speedups = [r["avg_speedup"] for r in results]
        all_durations = [r["duration"] for r in results]

        summary_stats["overall"] = {
            "avg_processing_time": statistics.mean(all_processing_times),
            "avg_real_time_factor": statistics.mean(all_rtfs),
            "avg_speedup": statistics.mean(all_speedups),
            "total_audio_duration": sum(all_durations),
            "total_processing_time": sum(all_processing_times) * num_runs,
            "benchmark_date": datetime.now().isoformat(),
        }

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = os.path.join(OUTPUT_DIR, f"benchmark_results_{timestamp}.json")

    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": summary_stats,
                "detailed_results": results,
                "config": {
                    "api_url": API_URL,
                    "test_audio_dir": TEST_AUDIO_DIR,
                    "num_runs": num_runs,
                },
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"\n{'=' * 60}")
    print(f"Benchmark completed!")
    print(f"Results saved to: {results_file}")

    # Print final summary
    if results:
        print(f"\nOverall Summary:")
        print(f"  Files tested: {len(results)}")
        print(
            f"  Average Real Time Factor: {summary_stats['overall']['avg_real_time_factor']:.3f}"
        )
        print(f"  Average Speedup: {summary_stats['overall']['avg_speedup']:.1f}x")
        print(
            f"  Total audio duration: {summary_stats['overall']['total_audio_duration']:.1f}s"
        )
        print(
            f"  Total processing time: {summary_stats['overall']['total_processing_time']:.1f}s"
        )

    return results_file


def select_test_files(
    max_files: int = 5, min_duration: float = 5.0, max_duration: float = 300.0
):
    """Select audio files for benchmarking"""
    audio_files = []
    mp3_dir = Path(TEST_AUDIO_DIR)

    if not mp3_dir.exists():
        print(f"Error: Test audio directory not found: {TEST_AUDIO_DIR}")
        return []

    # Get all mp3 files
    all_mp3s = list(mp3_dir.glob("*.mp3"))

    if not all_mp3s:
        print(f"No MP3 files found in {TEST_AUDIO_DIR}")
        return []

    # Sort by file size (as proxy for duration)
    all_mp3s.sort(key=lambda x: x.stat().st_size)

    # Select a variety of files
    selected_files = []

    # Try to get short, medium, and long files
    for mp3_file in all_mp3s:
        try:
            duration = get_audio_duration(str(mp3_file))
            if min_duration <= duration <= max_duration:
                selected_files.append((str(mp3_file), duration))

                if len(selected_files) >= max_files:
                    break
        except:
            continue

    # If we don't have enough files meeting duration criteria, take what we have
    if len(selected_files) < max_files:
        for mp3_file in all_mp3s:
            file_path = str(mp3_file)
            if file_path not in [f[0] for f in selected_files]:
                try:
                    duration = get_audio_duration(file_path)
                    selected_files.append((file_path, duration))

                    if len(selected_files) >= max_files:
                        break
                except:
                    continue

    # Sort by duration for logical testing order
    selected_files.sort(key=lambda x: x[1])

    return [f[0] for f in selected_files[:max_files]]


if __name__ == "__main__":
    print("Parakeet TDT 0.6B V3 ONNX Benchmark")
    print("=" * 60)

    # Select test files
    test_files = select_test_files(max_files=5)

    if not test_files:
        print("No suitable test files found.")
        sys.exit(1)

    print(f"Selected {len(test_files)} test files:")
    for i, file_path in enumerate(test_files, 1):
        file_name = os.path.basename(file_path)
        duration = get_audio_duration(file_path)
        size_mb = get_file_size_mb(file_path)
        print(f"  {i}. {file_name} ({duration:.1f}s, {size_mb:.1f} MB)")

    print(f"\nRunning benchmark with 3 runs per file...")

    try:
        results_file = run_benchmark(test_files, num_runs=3)

        # Also generate a quick markdown summary
        with open(results_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        md_file = results_file.replace(".json", ".md")
        with open(md_file, "w", encoding="utf-8") as f:
            f.write("# Parakeet TDT 0.6B V3 ONNX Benchmark Results\n\n")
            f.write(
                f"Date: {data['summary'].get('overall', {}).get('benchmark_date', 'N/A')}\n\n"
            )

            if "overall" in data["summary"]:
                overall = data["summary"]["overall"]
                f.write("## Overall Performance\n\n")
                f.write(
                    f"- **Average Real Time Factor**: {overall['avg_real_time_factor']:.3f}\n"
                )
                f.write(f"- **Average Speedup**: {overall['avg_speedup']:.1f}x\n")
                f.write(
                    f"- **Total Audio Duration**: {overall['total_audio_duration']:.1f}s\n"
                )
                f.write(
                    f"- **Total Processing Time**: {overall['total_processing_time']:.1f}s\n\n"
                )

            f.write("## File-by-File Results\n\n")
            f.write(
                "| File | Duration (s) | Size (MB) | Avg Time (s) | RTF | Speedup |\n"
            )
            f.write(
                "|------|-------------|-----------|--------------|-----|---------|\n"
            )

            for file_name, stats in data["summary"].get("files", {}).items():
                f.write(
                    f"| {file_name} | {stats['duration']:.1f} | {stats['size_mb']:.1f} | {stats['avg_processing_time']:.2f} | {stats['avg_rtf']:.3f} | {stats['avg_speedup']:.1f}x |\n"
                )

            f.write("\n## Notes\n")
            f.write(
                "- RTF (Real Time Factor): Processing time / Audio duration (lower is better)\n"
            )
            f.write("- Speedup: Audio duration / Processing time (higher is better)\n")
            f.write("- Benchmark run with INT8 quantization on ONNX Runtime\n")

        print(f"\nMarkdown summary: {md_file}")

    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError during benchmark: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
