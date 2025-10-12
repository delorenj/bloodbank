# Source this in your shell profile
bb() { python -m bloodbank.cli "$@"; }

# Example: wrap Anthropic CLI (if installed as `claude`)
claude() { command claude "$@"; }   # keep original
cwrap() { bb wrap claude -- "$@"; } # instrumented alias

# Example usage:
#   echo "help me refactor X" | cwrap --model opus
