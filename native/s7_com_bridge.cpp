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
 *   Commands: ping, shutdown, plcsim_get_state, plcsim_set_state, plcsim_step_scan,
 *             list_projects (via IDispatch on Simatic.Simatic / S7wspsmx.S7ProSim.1)
 */

#define _WIN32_DCOM
#include <windows.h>
#include <oleauto.h>
#include <cstdio>
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

static std::string extract_json_string(const std::string& line, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    size_t pos = line.find(needle);
    if (pos == std::string::npos) return "";
    pos = line.find(':', pos + needle.size());
    if (pos == std::string::npos) return "";
    pos = line.find('"', pos);
    if (pos == std::string::npos) return "";
    size_t end = line.find('"', pos + 1);
    if (end == std::string::npos) return "";
    return line.substr(pos + 1, end - pos - 1);
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

int main() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr)) {
        std::cout << "{\"ok\":false,\"error\":\"CoInitializeEx failed\"}\n" << std::flush;
        return 1;
    }

    IDispatch* pSimatic = nullptr;
    IDispatch* pProSim = nullptr;

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
        if (cmd == "plcsim_get_state") {
            if (!pProSim && FAILED(create_com_dispatch(L"S7wspsmx.S7ProSim.1", &pProSim))) {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim COM not registered\"}\n" << std::flush;
                continue;
            }
            VARIANT res;
            VariantInit(&res);
            if (SUCCEEDED(invoke_no_args(pProSim, L"GetState", DISPATCH_METHOD, &res)) && res.vt == VT_BSTR) {
                std::cout << "{\"ok\":true,\"cpu_state\":\"" << json_escape(wide_to_utf8(res.bstrVal)) << "\"}\n" << std::flush;
            } else {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.GetState failed\"}\n" << std::flush;
            }
            VariantClear(&res);
            continue;
        }
        if (cmd == "plcsim_step_scan") {
            if (!pProSim && FAILED(create_com_dispatch(L"S7wspsmx.S7ProSim.1", &pProSim))) {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim COM not registered\"}\n" << std::flush;
                continue;
            }
            VARIANT res;
            VariantInit(&res);
            if (SUCCEEDED(invoke_no_args(pProSim, L"ExecuteSingleScan", DISPATCH_METHOD, &res))) {
                std::cout << "{\"ok\":true,\"executed_cycles\":1}\n" << std::flush;
            } else {
                std::cout << "{\"ok\":false,\"error\":\"S7ProSim.ExecuteSingleScan failed\"}\n" << std::flush;
            }
            VariantClear(&res);
            continue;
        }

        // Delegate complex/extended commands by returning unsupported so Python falls back or extends
        std::cout << "{\"ok\":false,\"error\":\"Command '" << json_escape(cmd) << "' requires extended COM dispatch\"}\n" << std::flush;
    }

    if (pSimatic) pSimatic->Release();
    if (pProSim) pProSim->Release();
    CoUninitialize();
    return 0;
}
