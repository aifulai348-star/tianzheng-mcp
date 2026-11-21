"""Load installed libraries using the original, normally labelled interpreter."""
import site
import runpy
site.addsitedir('D:/dsh/autocad-mcp/venv/Lib/site-packages')
runpy.run_module('tz_mcp', run_name='__main__')
