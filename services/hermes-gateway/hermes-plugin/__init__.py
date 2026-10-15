"""Hermes directory-plugin shim for the Bloodbank gateway platform.

This directory is the plugin. It is symlinked to ~/.hermes/plugins/bloodbank-platform
by ../hermes-plugin/install.sh. The adapter code is vendored beside this file
(``bloodbank_hermes_gateway`` -> ../bloodbank_hermes_gateway) so it survives
PM-managed environment rebuilds (``hermes update``), which drop anything that was
hand-pip-installed. Third-party deps (nats-py, PyYAML) are declared in plugin.yaml
``pip_dependencies`` so PM puts them in the shared environment.
"""

import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bloodbank_hermes_gateway import register  # noqa: E402,F401

__all__ = ["register"]
