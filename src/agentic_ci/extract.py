"""Extract structured JSON result from Claude's stream-json output.

Reads the stream-json capture file, extracts the final text content,
parses it as JSON, and writes individual fields to an output directory.
"""

import json
import os
import re
import sys


def extract_text_from_stream(stream_file):
    """Reassemble the full text content from stream-json deltas."""
    text = ""
    with open(stream_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if msg.get("type") != "stream_event":
                continue

            event = msg.get("event", {})
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text += delta.get("text", "")

    return text


def parse_result(text):
    """Parse the JSON result from Claude's text output.

    Claude may output explanatory text before/after the JSON, and may
    wrap the JSON in markdown code fences.
    """
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    brace_start = text.find("{")
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[brace_start : i + 1])
                    except json.JSONDecodeError:
                        pass
                    break

    return json.loads(text.strip())


def write_outputs(result, output_dir):
    """Write structured fields to individual files in output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    fields = {
        "recommendation": str(result.get("recommendation", "unknown")),
        "confidence": str(result.get("confidence", 0.0)),
        "severity": str(result.get("severity", "unknown")),
        "manipulation_detected": str(result.get("manipulation_detected", False)).lower(),
        "analysis": str(result.get("analysis", "")),
    }

    for field, value in fields.items():
        with open(os.path.join(output_dir, field), "w") as f:
            f.write(value)

    with open(os.path.join(output_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description="Extract JSON result from Claude stream output")
    parser.add_argument("stream_file", help="Path to stream-json capture file")
    parser.add_argument("output_dir", help="Directory to write extracted fields")
    parsed = parser.parse_args(args)

    text = extract_text_from_stream(parsed.stream_file)
    if not text:
        print("Error: no text content found in stream output", file=sys.stderr)
        sys.exit(1)

    try:
        result = parse_result(text)
    except json.JSONDecodeError as e:
        print(f"Error: could not parse JSON from Claude output: {e}", file=sys.stderr)
        print(f"Raw text:\n{text[:500]}", file=sys.stderr)
        sys.exit(1)

    write_outputs(result, parsed.output_dir)
    print(f"Result extracted to {parsed.output_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
