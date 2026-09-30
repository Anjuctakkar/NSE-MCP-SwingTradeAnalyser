@echo off
setlocal EnableExtensions

REM ------------------------------------------------------------
REM  test-nse-mcp.bat
REM  Tests both NSE MCP servers (no token needed, they use No Auth):
REM    1. Bhavcopy  : https://mcp.nseindia.in/bhavcopy/cm/mcp
REM    2. CM market : https://mcp.nseindia.in/cmmkt/mcp
REM  Sends an MCP "initialize" request to each and prints the
REM  HTTP status, time taken, headers and response body.
REM ------------------------------------------------------------

set "BODY=%TEMP%\nse_mcp_body.json"

> "%BODY%" echo {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bat-test","version":"1.0"}}}

call :probe "Bhavcopy"  "https://mcp.nseindia.in/bhavcopy/cm/mcp"
call :probe "CM market" "https://mcp.nseindia.in/cmmkt/mcp"

echo.
echo ============================================================
echo  How to read the results
echo ============================================================
echo  200       = server works, problem is in your connector code
echo  404       = wrong URL path
echo  406       = missing Accept header (application/json, text/event-stream)
echo  504/hang  = backend behind the load balancer is timing out (server side)
echo.
echo  If BOTH show 504, contact NSE support and quote the Akamai-GRN value.

del "%BODY%" >nul 2>&1
endlocal
pause
exit /b 0


:probe
set "NAME=%~1"
set "URL=%~2"
set "HEADERS=%TEMP%\nse_mcp_headers.txt"
set "RESP=%TEMP%\nse_mcp_response.txt"
del "%HEADERS%" "%RESP%" >nul 2>&1

echo.
echo ============================================================
echo  %NAME%
echo  %URL%
echo ============================================================

curl -s -S -m 30 -X POST "%URL%" ^
    -H "Content-Type: application/json" ^
    -H "Accept: application/json, text/event-stream" ^
    --data-binary "@%BODY%" ^
    -D "%HEADERS%" -o "%RESP%" ^
    -w "HTTP status: %%{http_code}   Time: %%{time_total}s\n"

if errorlevel 1 (
    echo [curl error %ERRORLEVEL%] 28 = timed out, 6 = DNS failure, 7 = cannot connect, 35/60 = TLS problem
)

echo.
echo --- Response headers ---
if exist "%HEADERS%" type "%HEADERS%"
echo --- Response body ---
if exist "%RESP%" type "%RESP%"
echo.
exit /b 0
