from mcp.server.fastmcp import FastMCP #type: ignore
import json
import logging
import requests
import auth

#--------------------------------------------Environment Global Variables--------------------------------------------#

DATA_API_BASE_URL = "https://api.transatel.com/network/data-session/api/data-session/imsi"
CDR_API_BASE_URL = "https://api.transatel.com/network/usage/api/cdr"
SIM_SEARCH_API_BASE_URL = "https://api.transatel.com/line-search-api/api/sim/search"
ATTACH_API_BASE_URL = "https://api.transatel.com/network/attach/api/history"
mcp = FastMCP("transatel-mcp", host="0.0.0.0", port=8000)


#--------------------------------------------Helper functions--------------------------------------------#

#helper function to parse data session response and handle 404 case
def helper_data_session(response: requests.Response) -> str:
    if response.status_code == 404:
        return json.dumps({"status": "inactive", "detail": "No active data session"})

    data = response.json()
    sessions = []
    for session in data.get("sessions", []):
        ps_info = session.get("PS-Information", {})
        tac = None
        for equipment in ps_info.get("User-Equipment-Info", []):
            if equipment.get("User-Equipment-Info-Type") == "IMEI":
                imei = equipment["User-Equipment-Info-Value"]
                tac = imei[:8] if imei else None
                break

        sessions.append({
            "startTime": session.get("startTime"),
            "lastUpdateTime": session.get("lastUpdateTime"),
            "mcc-mnc": ps_info.get("3GPP-GGSN-MCC-MNC"),
            "apn": ps_info.get("Called-Station-Id"),
            "ratType": ps_info.get("3GPP-RAT-Type"),
            "deviceTAC": tac,
        })

    return json.dumps({"status": "active", "sessions": sessions})


#helper function to parse CDR response and handle empty/error cases
def helper_cdr(response: requests.Response) -> str:
    if response.status_code != 200:
        return json.dumps({"error": f"API returned {response.status_code}", "detail": response.text})

    data = response.json()

    if data.get("totalElements", 0) == 0:
        return json.dumps({"status": "inactive", "detail": "No CDR records found", "totalElements": 0})

    records = []
    for entry in data.get("content", []):
        header = entry.get("header", {})
        session = entry.get("body", {}).get("dataSession", {})
        usage = session.get("usage", {})

        #ci is removed but can be added later to integrate it with here for geo location purposes
        records.append({
            "eventDate": header.get("eventDate"),
            "apn": session.get("apn"),
            "originCountry": session.get("originCountry"),
            "mcc": session.get("mcc"),
            "mnc": session.get("mnc"),
            "ratType": session.get("rat"),
            "deviceTAC": session.get("imei", "")[:8] if session.get("imei") else None,
            "requestType": session.get("request", {}).get("requestType"),
            "requestDate": session.get("request", {}).get("requestDate"),
            "serviceOutcome": session.get("serviceOutcome"),
            "usage": {
                "uplink": int(usage.get("uplink", 0)),
                "downlink": int(usage.get("downlink", 0)),
                "total": int(usage.get("total", 0)),
            },
        })

    return json.dumps({
        "status": "active",
        "totalElements": data.get("totalElements"),
        "records": records,
    })


#helper function to get simSerial from IMSI
def get_sim_serial(token: str, imsi: str) -> str:
    response = requests.get(
        SIM_SEARCH_API_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"primaryImsi": imsi}
    )
    response.raise_for_status()
    sims = response.json().get("sims", [])
    if not sims:
        raise ValueError(f"No SIM found for IMSI {imsi}")
    return sims[0]["simSerial"]


#helper function to parse network attach history response
def helper_network_attach(response: requests.Response, last_only: bool = False) -> str:
    if response.status_code != 200:
        return json.dumps({"error": f"API returned {response.status_code}", "detail": response.text})

    data = response.json()
    content = data.get("content", [])

    if not content:
        return json.dumps({"status": "no_data", "detail": "No location history found"})

    def parse_event(event: dict) -> dict:
        header = event.get("header", {})
        body = event.get("body", {})
        imei = body.get("imei", "")
        return {
            "eventDate": header.get("eventDate"),
            "eventType": header.get("eventType"),
            "mcc": body.get("mcc"),
            "mnc": body.get("mnc"),
            "operatorName": body.get("operatorName"),
            "country": body.get("iso3"),
            "deviceTAC": imei[:8] if imei else None,
        }

    if last_only:
        return json.dumps({"status": "ok", "lastAttach": parse_event(content[0])})

    records = [parse_event(e) for e in content]
    return json.dumps({
        "status": "ok",
        "totalElements": data.get("totalElements", len(records)),
        "records": records,
    })

#--------------------------------------------MCP Tools--------------------------------------------#

@mcp.tool()
async def get_data_session(imsi: str) -> str:
    """
    Check if a SIM has an ongoing data session.

    Returns session status (active/inactive) with connection details.

    Args:
        imsi: IMSI to query

    Response format to follow:
        If active:
            "Ongoing session: Yes
            | Started: <startTime> UTC
            | APN: <apn>
            | RAT: <ratType> (<mapped name>)
            | Network: <mcc-mnc> — <operator name> (resolve MCC-MNC to operator name)
            | Device TAC: <deviceTAC>"
        If inactive:
            "Ongoing session: No | No active data session found"

    RAT mapping: 1=UTRAN(3G), 2=GERAN(2G), 6=EUTRAN(4G), 11=NR(5G)
    Resolve MCC-MNC to operator name using your knowledge or web search.
    Do not add extra commentary. State the facts only.
    """
    token = auth.gen_token()
    data_url = f"{DATA_API_BASE_URL}/{imsi}"

    data_response = requests.get(
        data_url,
        headers={"Authorization": f"Bearer {token}"}
    )

    return helper_data_session(data_response)

@mcp.tool()
async def get_network_attach(imsi: str, last_only: bool = False) -> str:
    """
    Get location history for a SIM card via network attach events.

    Sorted by eventDate descending (most recent first).

    Args:
        imsi: IMSI to query
        last_only: If True, return only the most recent location event

    Response format to follow:
        If last_only=True (single result):
            "Last Location: <eventDate> UTC | Event: <eventType suffix> | Network: <mcc>-<mnc> — <operatorName> | Country: <country> | Device TAC: <deviceTAC>"

        If last_only=False (list), present as a table:
        | Date (UTC) | Event Type | Network | Country | Device TAC |

        Column rules:
            - Network: <mcc-mnc> — <operatorName>
            - Country: iso3 code
            - Device TAC: first 8 digits of body.imei

        Then state: "Total records: <totalElements>"

        If no data: "No location history found"
        If error: "Location query failed: <error detail>"

    Do not add extra commentary. State the facts only.
    """
    try:
        token = auth.gen_token()
        sim_serial = get_sim_serial(token, imsi)
        response = requests.get(
            ATTACH_API_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"simSerial": sim_serial, "sort": "-eventDate"}
        )
        return helper_network_attach(response, last_only=last_only)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    

@mcp.tool()
async def get_cdr(imsi: str) -> str:
    """
    Get network usage CDR (Call Detail Records) for a SIM card.

    Returns data communication history with usage details per session.

    Args:
        imsi: IMSI to query

    Response format to follow:
        If records exist, present as a table:
        | Date (UTC) | APN | Network | Country | RAT | Device TAC | Upload | Download | Total |

        Column rules:
            - Network: <mcc-mnc> — <operator name> (resolve MCC-MNC to operator name)
            - Country: use originCountry code (e.g. FR, US, DE)
            - RAT: <ratType> (<mapped name>)
            - Device TAC: show raw TAC value as-is
            - Upload/Download/Total: convert bytes to human readable (KB/MB/GB)

        Then state: "Total records: <totalElements>"

        If no records: "No CDR records found — SIM is inactive or has no data history"
        If error: "CDR query failed: <error detail>"

    RAT mapping: 1=UTRAN(3G), 2=GERAN(2G), 6=EUTRAN(4G), 11=NR(5G)
    Resolve MCC-MNC to operator name using web search tool.
    Do not add extra commentary. State the facts only.

    When asked for a graph or visual, output a single self-contained HTML file using inline CSS and vanilla JS only (no external libraries).
    Always brand the header as "TRANSATEL NETWORK ANALYTICS". Use a dark theme with these colors:
        - Background: dark navy (#0d1117 / #161b22), font: monospace
        - Normal data: cyan (#00d4ff), Anomalies: pink (#ff2d78)
        - Upload: orange (#ff6b35), Download: green (#00e5a0)
    Include a "TRANSATEL NETWORK ANALYTICS" branded header, KPI summary cards, relevant charts, and a detail table.
    Output the full HTML inside a markdown html code block.
    """
    token = auth.gen_token()
    cdr_url = f"{CDR_API_BASE_URL}"
    cdr_response = requests.get(
        cdr_url,
        headers={"Authorization": f"Bearer {token}"},
        params={"imsi": imsi}
    )

    return helper_cdr(cdr_response)

#--------------------------------------------MCP Prompts--------------------------------------------#

@mcp.prompt()
def troubleshoot_sim(imsi: str) -> str:
    return f"""You are a Transatel network troubleshooting assistant.
For IMSI: {imsi}, follow these steps IN ORDER. Do not skip steps.

STEP 1: Call get_cdr with the IMSI.
STEP 2: Call get_data_session with the IMSI.
STEP 3: Call get_network_attach with the IMSI.
STEP 4: Using ONLY the data from steps 1, 2, and 3, respond in this EXACT format:

---
## SIM Troubleshoot Summary — IMSI: {imsi}

**1. SIM Status:** Only reply in [Active / Inactive]
    - Active: use get_data_session if not 404 then there is an active session
    - Inactive: if get_data_session returns no active session

**2. Last Attachment:** [datetime or "No attachment found"]
    - Use the most recent eventDate from get_network_attach (content[0])
    - TAC: extract from body.imei (first 8 digits) of that same event

**3. Last Data Communication:** [datetime or "No data communication found"]
    - Use get_data_session for this field
    - Use the most latest last eventDate from CDR records
    - Include: APN, country (MCC/MNC), usage (total bytes), RAT type, TAC

**4. Ongoing Data Session:** [Yes / No]
    - Yes: if get_data_session returns status "active"
    - No: if get_data_session returns status "inactive"
    - If yes, include: APN, RAT type, session start time, TAC

**5. Total Data Usage:** [sum of all totalBytes from CDR records]
    - Total data usage is the sum of totalBytes across all CDR records 
    - Convert to human readable format (KB/MB/GB)
    - If no CDR records, state "No data usage found"
    
---

RULES:
- Answer ONLY these 4 points. No additional analysis.
- Use the exact format above. Do not deviate.
- Convert RAT types: 1=UTRAN(3G), 2=GERAN(2G), 6=EUTRAN(4G), 11=NR(5G)
- Convert bytes to human readable (KB/MB/GB) for usage
- All timestamps must be in UTC
- If any API call fails, state the error for that specific point and continue with the rest
"""

#--------------------------------------------MCP Resources--------------------------------------------#

@mcp.resource("instructions://response-guidelines")
def response_guidelines() -> str:
    return """Global response rules for all Transatel MCP tool outputs:
- Be concise. State facts only. No filler sentences.
- Always convert RAT types: 1=UTRAN(3G), 2=GERAN(2G), 6=EUTRAN(4G), 11=NR(5G)
- Always convert bytes to human readable: B, KB, MB, GB
- All timestamps in UTC
- Never expose sensitive data: MSISDN, full IMEI, IP addresses, SIM serial
- If a tool returns an error, state it clearly and move on
- Use tables for multi-record data, single lines for single values
"""


#--------------------------------------------Main--------------------------------------------#
if __name__ == "__main__":
    mcp.run(transport="sse")
