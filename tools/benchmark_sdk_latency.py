"""Retired one-shot SDK benchmark.

The app uses Fluid's delegated stock-CLI path; benchmarking a separate SDK
client no longer measures any supported runtime and could bypass credential
gates. Kept as an explicit diagnostic for old developer commands.
"""

if __name__ == "__main__":
    raise SystemExit("SDK benchmark retired: use Fluid delegated protocol tests or app trace timings.")
