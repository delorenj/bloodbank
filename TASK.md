---
modified: 2025-12-18T09:40:05-05:00
---
 🎯 ORCHESTRATOR SYNTHESIS: UNIFIED REPOSITORY CLEANUP PLAN

  Critical Cross-Cutting Findings

  All experts identified overlapping issues converging on these root problems:

  1. The claude_updates/ Directory Crisis (Identified by 4/5 experts)
  - Contains ~1,300 lines of duplicated code
  - Different pyproject.toml with conflicting build systems (hatchling vs
  setuptools)
  - Async vs sync implementations creating incompatibility
  - Hardcoded credentials that differ from root config

  2. Security Vulnerabilities (Identified by 2/5 experts)
  - .env contains plaintext credentials WITHOUT .env.example
  - workflow.json has hardcoded API keys and credentials
  - .gemini/settings.json exposes API key
  - No secrets management strategy documented

  3. Vestigial AI Agent Frameworks (Identified by 2/5 experts)
  - 5 different frameworks (~2.5MB): .hive-mind/, .crush/, .swarm/,
  .claude-flow/, .gemini/
  - Most untouched since October 2024
  - Creates massive clutter and confusion

  4. Documentation Chaos (Identified by all 5 experts)
  - 40+ markdown files across 6 locations
  - Duplicates: SKILL.md (2 versions), MIGRATION.md (2 versions), event schemas
  (3 versions)
  - No clear navigation or hierarchy
  - Missing critical docs (Getting Started, API Reference, Operations Guide)

  5. Infrastructure Gap (Identified by 2/5 experts)
  - kubernetes/deploy.yaml recently deleted without replacement
  - Zero Docker configurations despite documentation references
  - No local development environment setup

  📊 CONSOLIDATED PRIORITY MATRIX

  Based on expert consensus weighted by impact/effort ratio:

  ⚡ CRITICAL - DO TODAY (2-4 hours total)

  C1. Security Remediation (30 min)
  - Create .env.example from .env template
  - Move credentials from workflow.json → .env
  - Move API key from .gemini/settings.json → .env
  - Add to .gitignore: .env, workflow.json, .gemini/settings.json
  - Document required environment variables in README

  C2. Delete claude_updates/ Directory (30 min)
  - Archive to docs/archive/v1-claude-snapshot/ if needed for reference
  - Remove entire directory from git
  - Verify all useful features exist in main codebase
  - Impact: Removes 1,300+ lines of duplicate code, resolves config conflicts

  C3. Remove AI Framework Cruft (45 min)
  - Delete: .hive-mind/, .crush/, .swarm/, claude-flow, .claude-flow.pid
  - Add to .gitignore
  - Impact: Removes 2.5MB+ of vestigial code

  C4. Delete Obsolete Files (15 min)
  - event_producers/events/registry_old.py
  - test_command_flow.py, test_import_compatibility.py (move to tests/)
  - coordination/, memory/, log file
  - Git commit cleanup: pyproject.toml.backup, kubernetes/deploy.yaml
  - Impact: Removes ~2,000 lines of dead code

  C5. Update .gitignore (15 min)
  # Build artifacts
  .ruff_cache/
  htmlcov/
  .coverage
  .benchmarks/
  .pytest_cache/

  # AI agent artifacts
  .hive-mind/
  .crush/
  .swarm/
  claude-flow
  .claude-flow.pid
  logs/g3_session_*.json
  logs/context_window_*.txt

  # Environment files
  .env

  # Empty directories
  coordination/
  memory/

  ---
  🔥 HIGH - DO THIS WEEK (6-8 hours total)

  H1. Consolidate pyproject.toml (2 hours)
  Merge best practices from both versions:
  - Keep root as source of truth
  - Add missing version constraints: fastapi>=0.100.0, uvicorn>=0.23.0,
  typer>=0.9.0, httpx>=0.24.0
  - Add [project.optional-dependencies] for dev tools
  - Add [tool.black], [tool.ruff], [tool.mypy] configurations
  - Delete tests/requirements-test.txt after migration
  - Update CI/CD to use uv pip install -e ".[dev]"

  H2. Move Root Modules to Package (2 hours)
  mkdir -p event_producers/core
  git mv config.py event_producers/core/
  git mv correlation_tracker.py event_producers/core/
  git mv rabbit.py event_producers/core/
  Update all imports throughout codebase (use IDE find/replace)

  H3. Documentation Quick Wins (2 hours)
  - Create docs/README.md as documentation hub/index
  - DELETE duplicate files:
    - claude_updates/SKILL.md (keep claude_skills version)
    - claude_updates/MIGRATION_v1_to_v2.md (keep docs version)
  - Fix docs/Bloodbank_Event_Schemas.md (complete or delete)
  - Move root docs to proper locations:
    - EventDrivenArchitecture.md → docs/architecture/
    - TASK.md → docs/development/

  H4. Fix Test Organization (1 hour)
  mkdir -p tests/{unit,integration}
  git mv test_command_flow.py tests/integration/
  git mv test_import_compatibility.py tests/unit/
  git mv subscriber_example.py event_producers/examples/

  H5. Scripts Reorganization (1 hour)
  mkdir -p scripts/rabbitmq
  git mv event_producers/scripts/setup_rabbitmq.sh scripts/rabbitmq/
  git mv event_producers/scripts/enable_rabbitmq_management.sh scripts/rabbitmq/
  git mv event_producers/scripts/fix_rabbitmq_management.sh scripts/rabbitmq/
  # Consolidate into single setup script if possible

  ---
  🟡 MEDIUM - DO THIS SPRINT (8-12 hours total)

  M1. Restore Infrastructure (4 hours)
  - Create docker/ directory with Dockerfile
  - Create docker-compose.yml for local stack (RabbitMQ + Redis + Bloodbank)
  - Restore kubernetes/ with base/overlays structure
  - Document deployment strategies

  M2. Documentation Consolidation (4 hours)
  Implement recommended structure:
  docs/
  ├── README.md (hub)
  ├── guides/ (Getting Started, Deployment, Development)
  ├── reference/ (Architecture, API, Events, Config)
  ├── integration/ (n8n, python, mcp)
  ├── troubleshooting/ (FAQ, Debugging, Common Issues)
  └── archive/ (threads, old reports)

  M3. Consolidate Envelope Creation (2 hours)
  - Keep ONLY event_producers/events/envelope.py:create_envelope()
  - DELETE utils.py:create_envelope() and base.py:create_envelope()
  - Update imports

  M4. N8N Integration Cleanup (2 hours)
  event_producers/integrations/n8n/
    workflows/ (JSON files)
    nodes/ (JS transformers)
  docs/integrations/n8n/
    README.md, setup.md, quickref.md
  tests/integration/
    test_rabbitmq.py (moved from n8n/)

  ---
  🟢 LOW - NICE TO HAVE (10-15 hours total)

  L1. Full Clean Architecture Refactoring (8 hours)
  - Implement layered structure: domain/ → application/ → infrastructure/ →
  interfaces/
  - Extract business logic from HTTP handlers
  - Proper dependency injection

  L2. Comprehensive Test Suite (4 hours)
  - Unit tests for all modules
  - Integration tests for event flows
  - E2E tests for complete workflows

  L3. Enhanced .mise.toml (1 hour)
  - Add task definitions (dev, test, lint, format)
  - Tool version management

  L4. Documentation Polish (2 hours)
  - Standardize formatting across all docs
  - Add TOCs to long documents
  - Create comprehensive FAQ
  - Add troubleshooting guides

  ---
  🎯 RECOMMENDED EXECUTION SEQUENCE

  Day 1 (Today): Execute all CRITICAL items
  - Security remediation
  - Delete claude_updates/
  - Remove AI framework cruft
  - Delete obsolete files
  - Update .gitignore
  - Result: Safe, clean repository foundation

  Week 1: Execute all HIGH items
  - Consolidate pyproject.toml
  - Move root modules to package
  - Documentation quick wins
  - Fix test organization
  - Scripts reorganization
  - Result: Single source of truth, proper structure

  Week 2-3: Execute MEDIUM items
  - Restore infrastructure
  - Documentation consolidation
  - Consolidate envelope creation
  - N8N integration cleanup
  - Result: Complete, well-organized repository

  Month 2+: Execute LOW items (as needed)
  - Architectural refactoring
  - Comprehensive testing
  - Documentation polish
  - Result: Production-ready, maintainable system

  ---
  📈 EXPECTED IMPACT

  File Reductions:
  - Delete ~50 files (AI frameworks, duplicates, obsolete)
  - Remove ~3,500 lines of redundant code
  - Reduce repository size by ~3MB

  Quality Improvements:
  - Single source of truth for configs
  - Clear documentation hierarchy
  - Proper code organization
  - Security hardening
  - Better developer experience

  Total Estimated Effort: 26-39 hours across all priorities
  Critical Path: 2-4 hours today for safety and security
  Value Unlocked: Maintainable, professional, secure codebase

  ---
  🤝 NEXT STEPS

  Would you like me to:
  1. Execute the CRITICAL cleanup now (automated with git commands)
  2. Generate the consolidated pyproject.toml file
  3. Create the missing .env.example template
  4. Start with a specific priority section
