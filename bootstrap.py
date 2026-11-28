"""Run the CLI with an optional additional dependency directory."""
import os
import runpy
import site

if __name__ == "__main__":
    extra = os.environ.get("TZ_EXTRA_SITE_PACKAGES")
    if extra:
        site.addsitedir(extra)
    runpy.run_module("tz_mcp", run_name="__main__")
