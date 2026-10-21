import Darwin
import Foundation
import AppKit
import Security

/// Minimal Keychain bridge for the Discord Option Tailer private Collector.
///
/// Secret-bearing modes require an inherited write-only pipe on file descriptor 3.
/// They never write secret bytes to stdout, stderr, argv, or the environment.
private enum OpenWoofKeychain {
    private static let ingestService = "openwoof/openwoof.javadevjt.tech/truenas-192-168-0-2/discord-option-tailer/production"
    private static let ingestAccount = "discord-option-tailer-collector"
    private static let nasService = "codex-truenas-mcp"
    private static let nasAccount = "joshuaterk"
    private static let secretFileDescriptor: Int32 = 3
    private static let maximumSecretBytes = 4096

    private enum Failure: Error {
        case safeCode(String)
    }

    private enum Lookup {
        case missing
        case found(Data?)
    }

    static func main() -> Never {
        do {
            try run()
            exit(EXIT_SUCCESS)
        } catch let Failure.safeCode(code) {
            writeFailure(code)
            exit(EXIT_FAILURE)
        } catch {
            writeFailure("internal-error")
            exit(EXIT_FAILURE)
        }
    }

    private static func run() throws {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard arguments.count == 1 else {
            throw Failure.safeCode("usage")
        }

        switch arguments[0] {
        case "status-ingest":
            try statusIngest()
        case "read-nas-to-fd":
            try requirePrivatePipe()
            var secret = try readExisting(service: nasService, account: nasAccount)
            try emitSecret(&secret, mode: "read-nas-to-fd", service: nasService, account: nasAccount)
        case "read-ingest-to-fd":
            try requirePrivatePipe()
            var secret = try readExisting(service: ingestService, account: ingestAccount)
            try emitSecret(&secret, mode: "read-ingest-to-fd", service: ingestService, account: ingestAccount)
        case "prompt-ingest-to-fd":
            try requirePrivatePipe()
            var secret: Data
            var source: String
            switch try lookup(service: ingestService, account: ingestAccount, includeData: true) {
            case .missing:
                secret = try promptIngestKey()
                try addNewIngestKey(secret)
                source = "created"
            case .found(let data):
                guard let data else {
                    throw Failure.safeCode("keychain-entry-invalid")
                }
                secret = try validatedSecret(data)
                source = "existing"
            }
            try emitSecret(&secret, mode: "prompt-ingest-to-fd", service: ingestService, account: ingestAccount, source: source)
        case "--self-test":
            try runSelfTest()
        case "--test-ipc":
            try requirePrivatePipe()
            var synthetic = Data("synthetic-ipc-token-7b9e2c41".utf8)
            try emitSecret(&synthetic, mode: "test-ipc", service: "synthetic", account: "synthetic")
        default:
            throw Failure.safeCode("usage")
        }
    }

    private static func statusIngest() throws {
        let exists: Bool
        switch try lookup(service: ingestService, account: ingestAccount, includeData: false) {
        case .missing:
            exists = false
        case .found:
            exists = true
        }
        try writeJSON([
            "ok": true,
            "mode": "status-ingest",
            "service": ingestService,
            "account": ingestAccount,
            "exists": exists,
        ])
    }

    /// Require a pipe or stream socketpair before any Keychain read or UI prompt.
    /// Node-style inherited IPC descriptors can be socketpairs on macOS. This
    /// prevents accidental direct invocation from writing a credential to a terminal or file.
    private static func requirePrivatePipe() throws {
        var descriptorInfo = stat()
        guard fstat(secretFileDescriptor, &descriptorInfo) == 0 else {
            throw Failure.safeCode("fd3-private-pipe-required")
        }
        let descriptorType = descriptorInfo.st_mode & mode_t(S_IFMT)
        let isPipe = descriptorType == mode_t(S_IFIFO)
        let isSocket = descriptorType == mode_t(S_IFSOCK)
        guard isPipe || isSocket else {
            throw Failure.safeCode("fd3-private-pipe-required")
        }
        let descriptorFlags = fcntl(secretFileDescriptor, F_GETFL)
        let accessMode = descriptorFlags & O_ACCMODE
        guard descriptorFlags >= 0,
              (accessMode == O_WRONLY || (isSocket && accessMode == O_RDWR)) else {
            throw Failure.safeCode("fd3-private-pipe-required")
        }
        if isSocket {
            var socketType: Int32 = 0
            var socketTypeLength = socklen_t(MemoryLayout<Int32>.size)
            guard getsockopt(secretFileDescriptor, SOL_SOCKET, SO_TYPE, &socketType, &socketTypeLength) == 0,
                  socketType == SOCK_STREAM else {
                throw Failure.safeCode("fd3-private-pipe-required")
            }
        }
    }

    private static func lookup(service: String, account: String, includeData: Bool) throws -> Lookup {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnAttributes as String: true,
        ]
        if includeData {
            query[kSecReturnData as String] = true
        }

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return .missing
        }
        guard status == errSecSuccess,
              let attributes = result as? [String: Any],
              attributes[kSecAttrService as String] as? String == service,
              attributes[kSecAttrAccount as String] as? String == account else {
            throw Failure.safeCode("keychain-read")
        }

        if includeData {
            guard let data = attributes[kSecValueData as String] as? Data else {
                throw Failure.safeCode("keychain-entry-invalid")
            }
            return .found(try validatedSecret(data))
        }
        return .found(nil)
    }

    private static func readExisting(service: String, account: String) throws -> Data {
        switch try lookup(service: service, account: account, includeData: true) {
        case .missing:
            throw Failure.safeCode("keychain-entry-missing")
        case .found(let data):
            guard let data else {
                throw Failure.safeCode("keychain-entry-invalid")
            }
            return try validatedSecret(data)
        }
    }

    private static func promptIngestKey() throws -> Data {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        application.activate(ignoringOtherApps: true)
        let source = """
        display dialog ("Enter the raw OpenWoof ingest key for Discord Option Tailer on TrueNAS 192.168.0.2, production. Required grants: collector:write, traces:write, metrics:write, logs:write. Use finite expiry unless you explicitly choose Never in OpenWoof. This does not create a key." & return & "Service: openwoof/openwoof.javadevjt.tech/truenas-192-168-0-2/discord-option-tailer/production" & return & "Account: discord-option-tailer-collector") default answer "" with hidden answer buttons {"Cancel", "Save Key"} default button "Save Key" cancel button "Cancel" with title "OpenWoof ingest key for Discord Option Tailer"
        return text returned of result
        """
        guard let script = NSAppleScript(source: source) else {
            throw Failure.safeCode("prompt-unavailable")
        }

        var scriptError: NSDictionary?
        let result = script.executeAndReturnError(&scriptError)
        if scriptError == nil, let answer = result.stringValue {
            return try validatedSecret(Data(answer.utf8))
        }
        if scriptError?[NSAppleScript.errorNumber] as? Int == -128 {
            throw Failure.safeCode("prompt-cancelled")
        }
        // Some command-line hosts cannot present Standard Additions dialogs.
        // The native fallback keeps the hidden value inside this same process.
        let alert = NSAlert()
        alert.messageText = "OpenWoof token for Discord Option Tailer"
        alert.informativeText = "TrueNAS 192.168.0.2 · production\nEnter an existing or admin-created raw token with collector:write, traces:write, metrics:write and logs:write. Prefer finite expiry; Never must be explicitly chosen in OpenWoof. It is saved only in the exact app Keychain entry and protected NAS file. No OpenWoof key is created by this helper."
        let input = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 480, height: 26))
        alert.accessoryView = input
        alert.addButton(withTitle: "Save Key")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = input
        guard alert.runModal() == .alertFirstButtonReturn else {
            throw Failure.safeCode("prompt-cancelled")
        }
        return try validatedSecret(Data(input.stringValue.utf8))
    }

    private static func addNewIngestKey(_ secret: Data) throws {
        try validateSecret(secret)
        let item: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: ingestService,
            kSecAttrAccount as String: ingestAccount,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlocked,
            kSecValueData as String: secret,
        ]
        let status = SecItemAdd(item as CFDictionary, nil)
        if status == errSecDuplicateItem {
            throw Failure.safeCode("keychain-entry-raced")
        }
        guard status == errSecSuccess else {
            throw Failure.safeCode("keychain-add")
        }
    }

    private static func validatedSecret(_ data: Data) throws -> Data {
        try validateSecret(data)
        return data
    }

    private static func validateSecret(_ data: Data) throws {
        guard !data.isEmpty, data.count <= maximumSecretBytes,
              let text = String(data: data, encoding: .utf8),
              !text.hasPrefix("Bearer "),
              !text.contains(" "),
              !text.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) else {
            throw Failure.safeCode("invalid-secret")
        }
    }

    private static func emitSecret(
        _ secret: inout Data,
        mode: String,
        service: String,
        account: String,
        source: String? = nil
    ) throws {
        try validateSecret(secret)
        defer {
            if !secret.isEmpty {
                secret.resetBytes(in: 0..<secret.count)
            }
        }

        try writeAll(secret, to: secretFileDescriptor)
        var response: [String: Any] = [
            "ok": true,
            "mode": mode,
            "service": service,
            "account": account,
        ]
        if let source {
            response["source"] = source
        }
        try writeJSON(response)
    }

    private static func writeAll(_ data: Data, to descriptor: Int32) throws {
        try data.withUnsafeBytes { buffer in
            guard let baseAddress = buffer.baseAddress else {
                throw Failure.safeCode("ipc-write")
            }
            var offset = 0
            while offset < buffer.count {
                let written = Darwin.write(descriptor, baseAddress.advanced(by: offset), buffer.count - offset)
                if written < 0 {
                    if errno == EINTR {
                        continue
                    }
                    throw Failure.safeCode("ipc-write")
                }
                guard written > 0 else {
                    throw Failure.safeCode("ipc-write")
                }
                offset += written
            }
        }
    }

    private static func writeJSON(_ object: [String: Any]) throws {
        var line = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        line.append(0x0A)
        try writeAll(line, to: STDOUT_FILENO)
    }

    private static func writeFailure(_ code: String) {
        let bytes = Data("openwoof-keychain: \(code)\n".utf8)
        _ = bytes.withUnsafeBytes { buffer in
            guard let baseAddress = buffer.baseAddress else { return 0 }
            return Darwin.write(STDERR_FILENO, baseAddress, buffer.count)
        }
    }

    private static func runSelfTest() throws {
        var checks = 0
        let exactBytes = Data("synthetic-self-test-token".utf8)
        try validateSecret(exactBytes)
        guard exactBytes == Data("synthetic-self-test-token".utf8) else {
            throw Failure.safeCode("self-test-byte-preservation")
        }
        checks += 1

        let boundary = Data(repeating: 0x41, count: maximumSecretBytes)
        try validateSecret(boundary)
        checks += 1

        for invalid in [
            Data(),
            Data("synthetic\nvalue".utf8),
            Data("synthetic\rvalue".utf8),
            Data("synthetic\u{0085}value".utf8),
            Data(repeating: 0x41, count: maximumSecretBytes + 1),
            Data([0xFF]),
        ] {
            var rejected = false
            do {
                try validateSecret(invalid)
            } catch {
                rejected = true
            }
            guard rejected else {
                throw Failure.safeCode("self-test-invalid-secret-accepted")
            }
            checks += 1
        }

        try writeJSON(["ok": true, "mode": "self-test", "checks": checks])
    }
}

OpenWoofKeychain.main()
