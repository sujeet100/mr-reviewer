#!/usr/bin/env bash
# Print the mr-review plugin root, derived from this script's own location.
#
# The skill needs an absolute path to charter/ and cannot assume $CLAUDE_PLUGIN_ROOT is
# exported into a skill's Bash calls. A script knows where it lives, so asking it is more
# reliable than reconstructing the path from the skill's own location by hand.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
