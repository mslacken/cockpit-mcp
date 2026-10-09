# cockpit-mcp — portable Cockpit bundle extractor + MCP server

Contents:
  cockpit_mcp_server_ng.py   main entry point (CLI + MCP server)
  cockpit_bridge.py          bridge backend (Call/Request/perform)
  cockpit_list_tools.py      host connect / asset fetch (stdlib only)
  parser/                    Node shim: parse.js + @babel deps (bundled node_modules)
  COCKPIT_JSDIS_SPEC.md      the record contract (reference)
  requirements.txt           Python deps

Requirements:
  - Python 3.10+
  - node (or point COCKPIT_MCP_NODE at a node binary)
  - pip install -r requirements.txt

Setup:
  tar xzf cockpit-mcp.tar.gz
  cd cockpit-mcp
  pip install -r requirements.txt

Usage:
  ./cockpit_mcp_server_ng.py --records --bundle /path/page.js
  ./cockpit_mcp_server_ng.py --fetch --insecure https://host:9090 --into /tmp/live
  ./cockpit_mcp_server_ng.py --mcp --bundle /path/page.js     # serve over stdio
  ./cockpit_mcp_server_ng.py --version

MCP client config (stdio):
  {
    "mcpServers": {
      "cockpit": {
        "command": "python3",
        "args": ["/path/to/cockpit-mcp/cockpit_mcp_server_ng.py", "--mcp",
                 "--bundle", "/path/page.js"]
      }
    }
  }

Notes:
  - parser/node_modules ships @babel/parser + @babel/traverse; if you strip
    node_modules, run `npm install` inside parser/ and set
    COCKPIT_JSDIS_PARSE_JS=/path/to/parser/parse.js
  - Optional modules degrade gracefully: without cockpit_bridge.py the invoke
    tools report an error; parsing and records still work.
