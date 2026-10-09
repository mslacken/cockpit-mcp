# cockpit-mcp — portable Cockpit bundle extractor + MCP server

This is more or less a vibe coded project!

The idea is that the [Cockpit](https://www.cockpit-project.org/) project
allows structured access to a Linux host for administration. This means
people have curated which commands are sensible to run and which are not.
An MCP server can leverage this knowledge to help with system administration.

How it works:
  The project is a two-program pipeline implementing the record contract in
  COCKPIT_JSDIS_SPEC.md:

    1. Fetch — cockpit_list_tools.py talks to a running Cockpit host the way
       a browser does (login, WebSocket handshake, manifests.json) and
       downloads the page JavaScript bundles.

    2. Parse — instead of executing the bundles (which the spec forbids),
       parser/parse.js (Node + @babel/parser + @babel/traverse) statically
       walks the AST and recovers every bridge-mutating call site
       (cockpit.spawn, dbus calls, proxy calls, file writes) as a JSON record.

    3. Model — cockpit_mcp_server_ng.py ingests those records, models them
       (Node/Record classes), dedupes them, and renders them per the spec.

    4. Bridge — record_to_calls() maps each record to concrete
       cockpit_bridge.Call objects; cockpit_bridge.py performs them over the
       cockpit-bridge session WebSocket directly — no browser needed.

    5. Serve — the CLI exposes the pipeline one-shot (--records, --recipe),
       and --mcp serves the same functions as MCP tools (cockpit_server,
       cockpit_overview, cockpit_parse, cockpit_records,
       cockpit_describe_record, cockpit_invoke). Plans that may change the
       host are shown first and only run with confirm=true.

  cockpit_bridge.py is optional: parsing and record listing work without it;
  only cockpit_invoke needs a live bridge connection.

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
