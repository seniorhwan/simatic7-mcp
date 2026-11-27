/*
 * s7_com_bridge.cpp — Native 32-bit (x86) Persistent COM Worker for Siemens STEP 7 Classic
 * ========================================================================================
 * Optional ultra-low-latency (<3 ms) native replacement for the persistent PowerShell
 * 32-bit COM daemon. Links directly to ole32.lib + oleaut32.lib (zero third-party deps).
 *
 * Build with MSVC x86 Developer Command Prompt:
 *   cl /nologo /O2 /EHsc /MT s7_com_bridge.cpp ole32.lib oleaut32.lib /Fe:s7_com_bridge.exe
 *
 * Protocol: Newline-delimited JSON over stdin/stdout.
 *   Startup banner: {"ok":true,"ready":true,"worker":"native_cpp_x86","pid":1234}
 *   Commands: ping, shutdown, plcsim_start_simulation, plcsim_stop_simulation,
 *             plcsim_get_state, plcsim_set_state, plcsim_step_scan
 *             (extended SIMATIC Manager commands return fallback_required=true so Python
 *              routes them to the persistent 32-bit PowerShell COM worker).
 */

#define _WIN32_DCOM
#include <windows.h>
#include <oleauto.h>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>

static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c >= 0x20) out.push_back(static_cast<char>(c));
                break;
        }
    }
    return out;
}

static std::string wide_to_utf8(const BSTR bstr) {
    if (!bstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, bstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 1) return "";
    std::string res(static_cast<size_t>(len - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, bstr, -1, &res[0], len - 1, nullptr, nullptr);
    return res;
}

static BSTR utf8_to_bstr(const std::string& s) {
    if (s.empty()) return SysAllocString(L"");
    int wlen = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    if (wlen <= 1) return SysAllocString(L"");
    std::wstring ws(static_cast<size_t>(wlen - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &ws[0], wlen - 1);
    return SysAllocStringLen(ws.c_str(), static_cast<UINT>(ws.size()));
}

// Skip whitespace starting at pos
static void skip_ws(const std::string& s, size_t& pos) {
    while (pos < s.size() && std::isspace(static_cast<unsigned char>(s[pos]))) {
        ++pos;
    }
}

// Parse a JSON string starting at an opening '"' and advance pos past closing '"'
static bool parse_json_string_token(const std::string& s, size_t& pos, std::string& out) {
    if (pos >= s.size() || s[pos] != '"') return false;
    ++pos;
    out.clear();
    while (pos < s.size()) {
        char c = s[pos++];
        if (c == '\\') {
            if (pos >= s.size()) return false;
            char esc = s[pos++];
            switch (esc) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u':
                    if (pos + 4 <= s.size()) {
                        unsigned int cp = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s[pos + i];
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= static_cast<unsigned int>(h - '0');
                            else if (h >= 'a' && h <= 'f') cp |= static_cast<unsigned int>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') cp |= static_cast<unsigned int>(h - 'A' + 10);
                        }
                        pos += 4;
                        if (cp < 0x80) out.push_back(static_cast<char>(cp));
                        else out.push_back('?');
                    }
                    break;
                default:
                    out.push_back(esc);
                    break;
            }
        } else if (c == '"') {
            return true;
        } else {
            out.push_back(c);
        }
    }
    return false;
}

// Find the start position of the value for a JSON object key (respecting string boundaries and escapes)
static bool find_json_value_pos(const std::string& line, const std::string& key, size_t& valuePos) {
    size_t pos = 0;
    while (pos < line.size()) {
        if (line[pos] == '"') {
            std::string token;
            if (!parse_json_string_token(line, pos, token)) return false;
            skip_ws(line, pos);
            if (pos < line.size() && line[pos] == ':') {
                ++pos;
                skip_ws(line, pos);
                if (token == key) {
                    valuePos = pos;
                    return true;
                }
            }
        } else {
            ++pos;
        }
    }
    return false;
}

static std::string extract_json_string(const std::string& line, const std::string& key) {
    size_t valPos = 0;
    if (!find_json_value_pos(line, key, valPos)) return "";
    std::string out;
    if (!parse_json_string_token(line, valPos, out)) return "";
    return out;
}

static int extract_json_int(const std::string& line, const std::string& key, int defaultValue) {
    size_t valPos = 0;
    if (!find_json_value_pos(line, key, valPos)) return defaultValue;
    if (valPos >= line.size()) return defaultValue;
    char* endPtr = nullptr;
    long val = std::strtol(line.c_str() + valPos, &endPtr, 10);
    if (endPtr == line.c_str() + valPos) return defaultValue;
    return static_cast<int>(val);
}

static bool extract_json_bool(const std::string& line, const std::string& key, bool defaultValue) {
    size_t valPos = 0;
    if (!find_json_value_pos(line, key, valPos)) return defaultValue;
    if (line.compare(valPos, 4, "true") == 0) return true;
    if (line.compare(valPos, 5, "false") == 0) return false;
    return defaultValue;
}

static std::string to_upper_ascii(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    }
    return s;
}

static HRESULT create_com_dispatch(const wchar_t* progId, IDispatch** outDisp) {
    CLSID clsid;
    HRESULT hr = CLSIDFromProgID(progId, &clsid);
    if (FAILED(hr)) return hr;
    return CoCreateInstance(clsid, nullptr, CLSCTX_LOCAL_SERVER | CLSCTX_INPROC_SERVER, IID_IDispatch, reinterpret_cast<void**>(outDisp));
}

static HRESULT invoke_no_args(IDispatch* disp, const wchar_t* method, WORD flags, VARIANT* result) {
    DISPID dispid;
    LPOLESTR name = const_cast<LPOLESTR>(method);
    HRESULT hr = disp->GetIDsOfNames(IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &dispid);
    if (FAILED(hr)) return hr;
    DISPPARAMS params = { nullptr, nullptr, 0, 0 };
    return disp->Invoke(dispid, IID_NULL, LOCALE_USER_DEFAULT, flags, &params, result, nullptr, nullptr);
}

static HRESULT invoke_one_bstr(IDispatch* disp, const wchar_t* method, const std::string& argUtf8, VARIANT* result) {
    DISPID dispid;
    LPOLESTR name = const_cast<LPOLESTR>(method);
    HRESULT hr = disp->GetIDsOfNames(IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &dispid);
    if (FAILED(hr)) return hr;
    VARIANT arg;
    VariantInit(&arg);
    arg.vt = VT_BSTR;
    arg.bstrVal = utf8_to_bstr(argUtf8);
    DISPPARAMS params = { &arg, nullptr, 1, 0 };
    hr = disp->Invoke(dispid, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_METHOD, &params, result, nullptr, nullptr);
    VariantClear(&arg);
    return hr;
}

static HRESULT invoke_one_i4(IDispatch* disp, const wchar_t* method, LONG argVal, VARIANT* result) {
    DISPID dispid;
    LPOLESTR name = const_cast<LPOLESTR>(method);
    HRESULT hr = disp->GetIDsOfNames(IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &dispid);
    if (FAILED(hr)) return hr;
    VARIANT arg;
    VariantInit(&arg);
    arg.vt = VT_I4;
    arg.lVal = argVal;
    DISPPARAMS params = { &arg, nullptr, 1, 0 };
    hr = disp->Invoke(dispid, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_METHOD, &params, result, nullptr, nullptr);
    VariantClear(&arg);
    return hr;
}

static bool ensure_prosim_connected(IDispatch*& pProSim, bool& connected, bool forceConnect, std::string& errOut) {
    if (!pProSim && FAILED(create_com_dispatch(L"S7wspsmx.S7ProSim.1", &pProSim))) {
        errOut = "S7ProSim COM (S7wspsmx.S7ProSim.1) not registered";
        return false;
    }
    if (forceConnect || !connected) {
        VARIANT res;
        VariantInit(&res);
        HRESULT hr = invoke_no_args(pProSim, L"Connect", DISPATCH_METHOD, &res);
        VariantClear(&res);
        if (FAILED(hr)) {
            errOut = "S7ProSim.Connect failed";
            return false;
        }
        connected = true;
    }
    return true;
}

static std::string read_prosim_state(IDispatch* pProSim, bool& okOut) {
    VARIANT res;
    VariantInit(&res);
    std::string state;
    okOut = false;
    if (SUCCEEDED(invoke_no_args(pProSim, L"GetState", DISPATCH_METHOD, &res)) && res.vt == VT_BSTR) {
        state = wide_to_utf8(res.bstrVal);
        okOut = true;
    }
    VariantClear(&res);
    return state;
}

static std::string read_prosim_scan_mode(IDispatch* pProSim) {
    VARIANT res;
    VariantInit(&res);
    std::string label;
    if (SUCCEEDED(invoke_no_args(pProSim, L"GetScanMode", DISPATCH_METHOD, &res))) {
        if (res.vt == VT_I4) {
            label = (res.lVal == 0) ? "SINGLE" : ((res.lVal == 1) ? "CONTINUOUS" : std::to_string(res.lVal));
        } else if (res.vt == VT_I2) {
            label = (res.iVal == 0) ? "SINGLE" : ((res.iVal == 1) ? "CONTINUOUS" : std::to_string(res.iVal));
        } else if (res.vt == VT_BSTR) {
            label = wide_to_utf8(res.bstrVal);
        }
    }
    VariantClear(&res);
    return label;
}

int main() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr)) {
        std::cout << "{\"ok\":false,\"error\":\"CoInitializeEx failed\"}\n" << std::flush;
        return 1;
    }

    IDispatch* pProSim = nullptr;
    bool psimConnected = false;

    std::cout << "{\"ok\":true,\"ready\":true,\"worker\":\"native_cpp_x86\",\"pid\":" << GetCurrentProcessId() << "}\n" << std::flush;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::string cmd = extract_json_string(line, "cmd");

        if (cmd == "ping") {
            std::cout << "{\"ok\":true,\"worker\":\"native_cpp_x86\",\"pid\":" << GetCurrentProcessId() << "}\n" << std::flush;
            continue;
        }
        if (cmd == "shutdown") {
            std::cout << "{\"ok\":true,\"shutting_down\":true}\n" << std::flush;
            break;
        }
        if (cmd == "plcsim_start_simulation") {
            bool autoConnect = extract_json_bool(line, "auto_connect", true);
            std::string errMsg;
            if (!ensure_prosim_connected(pProSim, psimConnected, autoConnect, errMsg)) {
                std::cout << "{\"ok\":false,\"error\":\"" << json_escape(errMsg) << "\"}\n" << std::flush;
                continue;
            }
            bool prevOk = false;
            std::string prevState = read_prosim_state(pProSim, prevOk);
            std::string prevScan = read_prosim_scan_mode(pProSim);

            std::string reqScan = to_upper_ascii(extract_json_string(line, "scan_mode"));
            if (!reqScan.empty()) {
                LONG modeVal = (reqScan.find("SINGLE") != std::string::npos) ? 0 : 1;
                VARIANT smRes;
                VariantInit(&smRes);
                HRESULT smHr = invoke_one_i4(pProSim, L"SetScanMode", modeVal, &smRes);
                VariantClear(&smRes);
                if (FAILED(smHr)) {
                    std::cout << "{\"ok\":false,\"error\":\"S7ProSim.SetScanMode failed\"}\n" << std::flush;
                    continue;
                }
            }

            std::string runMode = to_upper_ascii(extract_json_string(line, "run_mode"));
            if (runMode.empty() || runMode == "RUN-P" || runMode == "RUNP") runMode = "RUN_P";
            if (runMode != "RUN" && runMode != "RUN_P") {
                std::cout << "{\"ok\":false,\"error\":\"Unsupported run_mode; expected RUN_P or RUN\"}\n" << std::flush;
                continue;
            }

            VARIANT setRes;
            VariantInit(&setRes);
            HRESULT setHr = invoke_one_bstr(pProSim, L"SetState", runMode, &setRes);
            VariantClear(&setRes);
            if (FAILED(setHr)) {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.SetState failed\"}\n" << std::flush;
                continue;
            }

            bool stateOk = false;
            std::string cpuState = read_prosim_state(pProSim, stateOk);
            std::string curScan = read_prosim_scan_mode(pProSim);
            std::string cpuUpper = to_upper_ascii(cpuState);
            for (char& ch : cpuUpper) { if (ch == '-') ch = '_'; }
            bool verified = stateOk && (cpuUpper == runMode || (runMode == "RUN_P" && cpuUpper == "RUN"));

            std::cout << "{\"ok\":true,\"action\":\"start\",\"com_progid\":\"S7wspsmx.S7ProSim.1\","
                      << "\"com_architecture\":\"32-bit\",\"connected\":" << (psimConnected ? "true" : "false")
                      << ",\"previous_state\":\"" << json_escape(prevState)
                      << "\",\"target_state\":\"" << json_escape(runMode)
                      << "\",\"cpu_state\":\"" << json_escape(cpuState)
                      << "\",\"previous_scan_mode\":\"" << json_escape(prevScan)
                      << "\",\"scan_mode\":\"" << json_escape(curScan)
                      << "\",\"verified\":" << (verified ? "true" : "false") << "}\n" << std::flush;
            continue;
        }
        if (cmd == "plcsim_stop_simulation") {
            bool autoConnect = extract_json_bool(line, "auto_connect", true);
            bool disconnectAfter = extract_json_bool(line, "disconnect", false);
            std::string errMsg;
            if (!ensure_prosim_connected(pProSim, psimConnected, autoConnect, errMsg)) {
                std::cout << "{\"ok\":false,\"error\":\"" << json_escape(errMsg) << "\"}\n" << std::flush;
                continue;
            }
            bool prevOk = false;
            std::string prevState = read_prosim_state(pProSim, prevOk);
            std::string prevScan = read_prosim_scan_mode(pProSim);

            VARIANT setRes;
            VariantInit(&setRes);
            HRESULT setHr = invoke_one_bstr(pProSim, L"SetState", "STOP", &setRes);
            VariantClear(&setRes);
            if (FAILED(setHr)) {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.SetState(STOP) failed\"}\n" << std::flush;
                continue;
            }

            bool stateOk = false;
            std::string cpuState = read_prosim_state(pProSim, stateOk);
            std::string curScan = read_prosim_scan_mode(pProSim);
            bool verified = stateOk && (to_upper_ascii(cpuState) == "STOP");

            if (disconnectAfter) {
                VARIANT discRes;
                VariantInit(&discRes);
                invoke_no_args(pProSim, L"Disconnect", DISPATCH_METHOD, &discRes);
                VariantClear(&discRes);
                psimConnected = false;
            }

            std::cout << "{\"ok\":true,\"action\":\"stop\",\"com_progid\":\"S7wspsmx.S7ProSim.1\","
                      << "\"com_architecture\":\"32-bit\",\"connected\":" << (psimConnected ? "true" : "false")
                      << ",\"disconnected\":" << (disconnectAfter ? "true" : "false")
                      << ",\"previous_state\":\"" << json_escape(prevState)
                      << "\",\"target_state\":\"STOP\",\"cpu_state\":\"" << json_escape(cpuState)
                      << "\",\"previous_scan_mode\":\"" << json_escape(prevScan)
                      << "\",\"scan_mode\":\"" << json_escape(curScan)
                      << "\",\"verified\":" << (verified ? "true" : "false") << "}\n" << std::flush;
            continue;
        }
        if (cmd == "plcsim_get_state") {
            std::string errMsg;
            if (!ensure_prosim_connected(pProSim, psimConnected, false, errMsg)) {
                std::cout << "{\"ok\":false,\"error\":\"" << json_escape(errMsg) << "\"}\n" << std::flush;
                continue;
            }
            bool stateOk = false;
            std::string cpuState = read_prosim_state(pProSim, stateOk);
            if (stateOk) {
                std::string scanMode = read_prosim_scan_mode(pProSim);
                std::cout << "{\"ok\":true,\"cpu_state\":\"" << json_escape(cpuState)
                          << "\",\"scan_mode\":\"" << json_escape(scanMode) << "\"}\n" << std::flush;
            } else {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.GetState failed\"}\n" << std::flush;
            }
            continue;
        }
        if (cmd == "plcsim_set_state") {
            std::string errMsg;
            if (!ensure_prosim_connected(pProSim, psimConnected, false, errMsg)) {
                std::cout << "{\"ok\":false,\"error\":\"" << json_escape(errMsg) << "\"}\n" << std::flush;
                continue;
            }
            std::string targetState = to_upper_ascii(extract_json_string(line, "state"));
            if (targetState.empty()) targetState = "RUN_P";
            if (targetState == "RUN-P" || targetState == "RUNP") targetState = "RUN_P";
            VARIANT res;
            VariantInit(&res);
            if (SUCCEEDED(invoke_one_bstr(pProSim, L"SetState", targetState, &res))) {
                bool stateOk = false;
                std::string cpuState = read_prosim_state(pProSim, stateOk);
                std::cout << "{\"ok\":true,\"cpu_state\":\"" << json_escape(stateOk ? cpuState : targetState) << "\"}\n" << std::flush;
            } else {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.SetState failed\"}\n" << std::flush;
            }
            VariantClear(&res);
            continue;
        }
        if (cmd == "plcsim_step_scan") {
            std::string errMsg;
            if (!ensure_prosim_connected(pProSim, psimConnected, false, errMsg)) {
                std::cout << "{\"ok\":false,\"error\":\"" << json_escape(errMsg) << "\"}\n" << std::flush;
                continue;
            }
            int cycles = extract_json_int(line, "cycles", 1);
            if (cycles < 1) cycles = 1;
            if (cycles > 1000) cycles = 1000;
            int executed = 0;
            bool ok = true;
            for (int i = 0; i < cycles; ++i) {
                VARIANT res;
                VariantInit(&res);
                if (SUCCEEDED(invoke_no_args(pProSim, L"ExecuteSingleScan", DISPATCH_METHOD, &res))) {
                    ++executed;
                } else {
                    ok = false;
                }
                VariantClear(&res);
                if (!ok) break;
            }
            if (ok) {
                std::cout << "{\"ok\":true,\"executed_cycles\":" << executed << "}\n" << std::flush;
            } else {
                std::cout << "{\"ok\":false,\"executed_cycles\":" << executed << ",\"error\":\"S7ProSim.ExecuteSingleScan failed\"}\n" << std::flush;
            }
            continue;
        }

        // Delegate complex/extended commands by signalling fallback_required so Python routes to persistent 32-bit PS COM worker
        std::cout << "{\"ok\":false,\"fallback_required\":true,\"error\":\"Command '" << json_escape(cmd) << "' delegated to extended COM worker\"}\n" << std::flush;
    }

    if (pProSim) pProSim->Release();
    CoUninitialize();
    return 0;
}
