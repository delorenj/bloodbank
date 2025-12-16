#!/usr/bin/env python3
"""
Backward Compatibility Test for Event Module Refactoring.

This script validates that all imports used by production code still work
after the modular refactoring of the events module.

Tests:
1. Imports used by http.py
2. Imports used by mcp_server.py
3. Additional core exports that should remain available
"""

import sys
from typing import Dict, List

# Color codes for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class ImportTest:
    """Test harness for validating imports."""

    def __init__(self):
        self.results: List[Dict[str, any]] = []
        self.total_tests = 0
        self.passed_tests = 0
        self.failed_tests = 0

    def test_import(self, module: str, names: List[str], description: str):
        """
        Test importing specific names from a module.

        Args:
            module: Module path (e.g., "event_producers.events")
            names: List of names to import (e.g., ["AgentThreadPrompt"])
            description: Human-readable description of what's being tested
        """
        self.total_tests += 1
        test_result = {
            "module": module,
            "names": names,
            "description": description,
            "status": "PASS",
            "error": None,
            "imported_objects": {}
        }

        try:
            # Dynamic import
            imported_module = __import__(module, fromlist=names)

            # Verify each name exists
            for name in names:
                if not hasattr(imported_module, name):
                    raise AttributeError(
                        f"Module '{module}' has no attribute '{name}'"
                    )
                obj = getattr(imported_module, name)
                test_result["imported_objects"][name] = {
                    "type": type(obj).__name__,
                    "module": getattr(obj, "__module__", "unknown")
                }

            self.passed_tests += 1

        except Exception as e:
            test_result["status"] = "FAIL"
            test_result["error"] = str(e)
            self.failed_tests += 1

        self.results.append(test_result)
        return test_result

    def print_results(self):
        """Print detailed test results."""
        print(f"\n{BOLD}{'=' * 80}{RESET}")
        print(f"{BOLD}Import Compatibility Test Results{RESET}")
        print(f"{BOLD}{'=' * 80}{RESET}\n")

        for idx, result in enumerate(self.results, 1):
            status_color = GREEN if result["status"] == "PASS" else RED
            status_symbol = "✓" if result["status"] == "PASS" else "✗"

            print(f"{BOLD}Test {idx}: {result['description']}{RESET}")
            print(f"  Module: {result['module']}")
            print(f"  Imports: {', '.join(result['names'])}")
            print(f"  Status: {status_color}{status_symbol} {result['status']}{RESET}")

            if result["status"] == "PASS":
                for name, info in result["imported_objects"].items():
                    print(f"    ✓ {name}: {info['type']} (from {info['module']})")
            else:
                print(f"    {RED}Error: {result['error']}{RESET}")

            print()

        # Summary
        print(f"{BOLD}{'=' * 80}{RESET}")
        print(f"{BOLD}Summary{RESET}")
        print(f"{BOLD}{'=' * 80}{RESET}")
        print(f"Total Tests: {self.total_tests}")
        print(f"{GREEN}Passed: {self.passed_tests}{RESET}")
        print(f"{RED}Failed: {self.failed_tests}{RESET}")

        if self.failed_tests == 0:
            print(f"\n{GREEN}{BOLD}✓ All imports are backward compatible!{RESET}\n")
            return 0
        else:
            print(f"\n{RED}{BOLD}✗ Some imports failed - backward compatibility is broken!{RESET}\n")
            return 1


def main():
    """Run all import compatibility tests."""
    tester = ImportTest()

    # Test 1: http.py imports
    tester.test_import(
        "event_producers.events",
        ["AgentThreadPrompt", "AgentThreadResponse"],
        "http.py imports (AgentThread events)"
    )

    # Test 2: mcp_server.py imports (these may fail if not migrated)
    tester.test_import(
        "event_producers.events",
        ["LLMPrompt", "LLMResponse", "Artifact", "envelope_for"],
        "mcp_server.py imports (LLM events - DEPRECATED)"
    )

    # Test 3: Core base types
    tester.test_import(
        "event_producers.events",
        ["EventEnvelope", "TriggerType", "Source", "AgentType", "AgentContext", "CodeState"],
        "Core event envelope types"
    )

    # Test 4: Fireflies events
    tester.test_import(
        "event_producers.events",
        [
            "FirefliesTranscriptUploadPayload",
            "FirefliesTranscriptReadyPayload",
            "FirefliesTranscriptProcessedPayload",
            "FirefliesTranscriptFailedPayload",
        ],
        "Fireflies domain events"
    )

    # Test 5: GitHub events
    tester.test_import(
        "event_producers.events",
        ["GitHubPRCreatedPayload"],
        "GitHub domain events"
    )

    # Test 6: Registry and utilities
    tester.test_import(
        "event_producers.events",
        ["EventRegistry", "get_registry", "register_event", "create_envelope"],
        "Event registry and utilities"
    )

    # Test 7: Direct domain imports (new modular structure)
    tester.test_import(
        "event_producers.events.domains.agent_thread",
        ["AgentThreadPrompt", "AgentThreadResponse", "AgentThreadErrorPayload"],
        "Direct import from agent_thread domain"
    )

    # Test 8: Direct domain imports for fireflies
    tester.test_import(
        "event_producers.events.domains.fireflies",
        [
            "FirefliesTranscriptReadyPayload",
            "TranscriptSentence",
            "AIFilters",
            "SentimentType"
        ],
        "Direct import from fireflies domain"
    )

    # Print results and return exit code
    exit_code = tester.print_results()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
