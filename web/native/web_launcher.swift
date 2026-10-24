import EventKit
import Foundation

// Start the web server as a child so macOS credits its calendar access to this launcher.
@main
struct WebLauncher {
    static let forwardedSignals = [SIGTERM, SIGINT, SIGHUP]
    static let usageExitStatus: Int32 = 64
    static let startFailureExitStatus: Int32 = 70
    static let signalExitOffset: Int32 = 128

    static func log(_ message: String) {
        FileHandle.standardError.write(Data("social-web: \(message)\n".utf8))
    }

    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard let program = arguments.first else {
            log("usage: social-web <program> [arguments...]")
            exit(usageExitStatus)
        }
        let server = Process()
        server.executableURL = URL(fileURLWithPath: program)
        server.arguments = Array(arguments.dropFirst())
        // launchd stops only this process, so pass stop signals to the server.
        let queue = DispatchQueue(label: "social-web.signals")
        let sources = forwardedSignals.map { number -> DispatchSourceSignal in
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: queue)
            source.setEventHandler { kill(server.processIdentifier, number) }
            source.resume()
            return source
        }
        do {
            try server.run()
        } catch {
            log("could not start \(program): \(error.localizedDescription)")
            exit(startFailureExitStatus)
        }
        // Ask once while the server runs; macOS remembers the answer for this launcher.
        if EKEventStore.authorizationStatus(for: .event) == .notDetermined {
            Task.detached {
                let granted = (try? await requestAccess(EKEventStore())) ?? false
                log(granted ? "calendar access allowed" : "calendar access not allowed")
            }
        }
        server.waitUntilExit()
        withExtendedLifetime(sources) {}
        let status = server.terminationStatus
        exit(server.terminationReason == .exit ? status : signalExitOffset + status)
    }

    static func requestAccess(_ store: EKEventStore) async throws -> Bool {
        if #available(macOS 14.0, *) {
            return try await store.requestFullAccessToEvents()
        }
        return try await store.requestAccess(to: .event)
    }
}
