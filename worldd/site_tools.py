"""Declared page tools, invoked inside the world-held browser context.

Declarations and returned data remain untrusted. BrowserService supplies the
lease and exact network-request authorization; a declaration grants nothing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import urldefrag

_DISCOVER = """async () => {
  const found = [];
  const api = navigator.modelContextTesting;
  if (api && typeof api.listTools === 'function') {
    let listed = []; try { listed = await api.listTools(); } catch (_) {}
    for (const item of (Array.isArray(listed) ? listed : []).slice(0, 128)) {
      if (!item || typeof item.name !== 'string') continue;
      found.push({tool_id:'webmcp:' + item.name, path:'webmcp', name:item.name,
        description:item.description||'', input_schema:item.inputSchema||{}, url:location.href});
    }
  }
  for (const [index, form] of [...document.querySelectorAll('form[toolname]')].entries()) {
    const properties = {}, required = [];
    for (const field of form.elements) {
      if (!field.name || field.disabled || ['password','hidden','file','submit'].includes(field.type)) continue;
      properties[field.name] = {type:field.type==='checkbox'?'boolean':'string'};
      if (field.required) required.push(field.name);
    }
    found.push({tool_id:'form:' + index + ':' + form.getAttribute('toolname'),
      path:'webmcp_form', name:form.getAttribute('toolname'),
      description:form.getAttribute('tooldescription')||'', url:form.action||location.href,
      method:(form.method||'GET').toUpperCase(), form_index:index,
      input_schema:{type:'object',properties,required,additionalProperties:false}});
  }
  for (const link of document.querySelectorAll('link[rel~="mcp"],link[rel~="mcp-server"]')) {
    found.push({tool_id:'endpoint:' + link.href, path:'site_mcp',
      name:link.title||'Site MCP endpoint', url:link.href,
      discovery:'declared_endpoint', input_schema:{type:'object'}});
  }
  return found.slice(0, 128);
}"""


class SiteToolError(ValueError):
    pass


class SiteTools:
    def __init__(self, handle):
        self.handle, self.page = handle, handle["page"]

    async def discover(self):
        try:
            result = await asyncio.wait_for(self.page.evaluate(_DISCOVER), 5)
        except TimeoutError:
            return []
        if not isinstance(result, list) or len(json.dumps(result)) > 1000000:
            return []
        tools = []
        for item in result:
            if len(tools) >= 128:
                break
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            if not 0 < len(item["name"]) <= 200:
                continue
            if isinstance(item.get("input_schema"), str):
                try:
                    item["input_schema"] = json.loads(item["input_schema"])
                except ValueError:
                    item["input_schema"] = {}
            item["description"] = str(item.get("description", ""))[:4000]
            if item.get("path") not in {"webmcp", "webmcp_form", "site_mcp"}:
                continue
            if not isinstance(item.get("input_schema"), dict):
                item["input_schema"] = {}
            tools.append(item)
            if item["path"] == "site_mcp":
                for declared in self.handle.get("site_mcp_catalogues", {}).get(item["url"], []):
                    if len(tools) >= 128:
                        break
                    tools.append(
                        {
                            "tool_id": "mcp:" + item["url"] + "#" + declared["name"],
                            "path": "site_mcp",
                            "name": declared["name"],
                            "url": item["url"],
                            "description": str(declared.get("description", ""))[:4000],
                            "input_schema": declared.get("inputSchema", {}),
                            "discovery": "tools/list",
                        }
                    )
        return tools

    async def describe(self, name, endpoint=None):
        tools = await self.discover()
        if endpoint:
            endpoint = urldefrag(endpoint)[0]
            declared = next(
                (
                    item
                    for item in tools
                    if item["path"] == "site_mcp" and urldefrag(item["url"])[0] == endpoint
                ),
                None,
            )
            if not declared:
                raise SiteToolError("The page has not declared this site MCP endpoint")
            return {**declared, "name": name, "tool_id": "mcp:" + name, "url": endpoint}
        selected = [
            item
            for item in tools
            if (item["path"] != "site_mcp" or item.get("discovery") == "tools/list")
            and name in {item["name"], item["tool_id"]}
        ]
        if len(selected) != 1:
            raise SiteToolError("Select one currently declared site tool by its tool_id")
        return selected[0]

    async def invoke(self, descriptor, arguments, *, authorize_rpc=None):
        if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 50000:
            raise SiteToolError("Site tool arguments must be a bounded object")
        path = descriptor["path"]
        if path == "webmcp":
            result = await self.page.evaluate(
                """async ({name, arguments: args}) => {
                const api = navigator.modelContextTesting;
                if (!api || typeof api.executeTool !== 'function') throw Error('WebMCP unavailable');
                return await api.executeTool(name, JSON.stringify(args));
            }""",
                {"name": descriptor["name"], "arguments": arguments},
            )
            if len(json.dumps(result)) > 50000:
                raise SiteToolError("The site tool result exceeds the browser observation limit")
            return {"value": result}
        if path == "webmcp_form":
            form = self.page.locator("form[toolname]").nth(descriptor["form_index"])
            fields = descriptor["input_schema"]["properties"]
            if set(arguments) - set(fields):
                raise SiteToolError("Arguments name an undeclared or private form field")
            for name, value in arguments.items():
                control = form.locator("[name=" + json.dumps(name) + "]")
                if await control.count() != 1:
                    raise SiteToolError("The declared form field is not unique")
                kind = (await control.get_attribute("type") or "").lower()
                if kind in {"hidden", "password", "file"}:
                    raise SiteToolError("Private form controls cannot be filled by a site call")
                if kind == "checkbox":
                    if type(value) is not bool:
                        raise SiteToolError("Checkbox values must be booleans")
                    await control.set_checked(value)
                elif isinstance(value, str):
                    if await control.evaluate("element => element.tagName") == "SELECT":
                        await control.select_option(value)
                    else:
                        await control.fill(value)
                else:
                    raise SiteToolError("Declared form inputs require string values")
            submit = form.locator("button[type=submit],button:not([type]),input[type=submit]")
            if await submit.count():
                await submit.first.click()
            else:
                await form.evaluate("form => form.requestSubmit()")
            return {"submitted": True}
        if path != "site_mcp" or authorize_rpc is None:
            raise SiteToolError("The declared site-tool path is unavailable")
        return await self._mcp(descriptor, arguments, authorize_rpc)

    async def _rpc(self, endpoint, method, parameters, authorize, *, notification=False):
        state = self.handle.setdefault("site_mcp_sessions", {}).setdefault(endpoint, {})
        call_id = None if notification else "world_" + secrets.token_hex(8)
        body = {"jsonrpc": "2.0", "method": method, "params": parameters}
        if call_id:
            body["id"] = call_id
        await authorize(endpoint, body)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if state.get("session_id"):
            headers["Mcp-Session-Id"] = state["session_id"]
        if state.get("protocol_version"):
            headers["MCP-Protocol-Version"] = state["protocol_version"]
        response = await self.page.evaluate(
            """async ({endpoint, body, headers}) => {
          const response = await fetch(endpoint,{method:'POST',headers,body:JSON.stringify(body)});
          const text = await response.text();
          if (text.length > 1000000) throw Error('Site MCP response too large');
          return {status:response.status,text,session_id:response.headers.get('Mcp-Session-Id')};
        }""",
            {"endpoint": endpoint, "body": body, "headers": headers},
        )
        if not 200 <= response["status"] < 300:
            raise SiteToolError("The declared MCP endpoint rejected the request")
        if response.get("session_id"):
            state["session_id"] = response["session_id"]
        if notification:
            return None
        packets = []
        try:
            if response["text"].lstrip().startswith("{"):
                packets.append(json.loads(response["text"]))
            else:
                for event in response["text"].replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(
                        line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                    )
                    if data:
                        packets.append(json.loads(data))
        except ValueError:
            raise SiteToolError(
                "The declared MCP endpoint returned invalid protocol data"
            ) from None
        matched = next(
            (
                packet
                for packet in packets
                if isinstance(packet, dict) and packet.get("id") == call_id
            ),
            None,
        )
        if not matched or "error" in matched or "result" not in matched:
            raise SiteToolError("No matching successful site MCP reply was received")
        return matched["result"]

    async def _mcp(self, descriptor, arguments, authorize):
        endpoint = descriptor["url"]
        state = self.handle.setdefault("site_mcp_sessions", {}).setdefault(endpoint, {})
        if not state.get("initialized"):
            negotiated = await self._rpc(
                endpoint,
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "LandingPartyWorld", "version": "0.1"},
                },
                authorize,
            )
            if not isinstance(negotiated, dict) or not isinstance(
                negotiated.get("protocolVersion"), str
            ):
                raise SiteToolError("The declared MCP endpoint did not negotiate a protocol")
            state["protocol_version"] = negotiated["protocolVersion"]
            await self._rpc(endpoint, "notifications/initialized", {}, authorize, notification=True)
            state["initialized"] = True
        listed = await self._rpc(endpoint, "tools/list", {}, authorize)
        tools = listed.get("tools", []) if isinstance(listed, dict) else None
        if (
            not isinstance(tools, list)
            or len(tools) > 128
            or any(not isinstance(item, dict) for item in tools)
        ):
            raise SiteToolError("The declared MCP tool catalogue is invalid or too large")
        if listed.get("nextCursor"):
            raise SiteToolError("The site MCP catalogue exceeds this session's bounded page")
        if any(
            not isinstance(item.get("name"), str) or not 0 < len(item["name"]) <= 200
            for item in tools
        ):
            raise SiteToolError("The declared MCP tool names are invalid")
        self.handle.setdefault("site_mcp_catalogues", {})[endpoint] = tools
        if descriptor["name"] == "__list_tools__":
            return {"tools": tools, "endpoint": endpoint}
        if not any(item.get("name") == descriptor["name"] for item in tools):
            raise SiteToolError("The endpoint has not declared the requested MCP tool")
        return await self._rpc(
            endpoint,
            "tools/call",
            {
                "name": descriptor["name"],
                "arguments": arguments,
            },
            authorize,
        )
