import EventKit
import Foundation

// Read calendar occurrences without changing events or reminders.
@main
struct CalendarReader {
    static func emit(_ data: [String: Any]) {
        if let json = try? JSONSerialization.data(withJSONObject: data),
           let text = String(data: json, encoding: .utf8) {
            print(text)
        }
    }

    static func main() async {
        let argumentCount = 3
        guard CommandLine.arguments.count == argumentCount else {
            emit(["status": "invalid_window"])
            return
        }
        let dates = DateFormatter()
        dates.locale = Locale(identifier: "en_US_POSIX")
        dates.calendar = Calendar(identifier: .gregorian)
        dates.timeZone = .current
        dates.dateFormat = "yyyy-MM-dd"
        guard let start = dates.date(from: CommandLine.arguments[1]),
              let end = dates.date(from: CommandLine.arguments[2]), start < end else {
            emit(["status": "invalid_window"])
            return
        }
        let store = EKEventStore()
        do {
            let granted: Bool
            if #available(macOS 14.0, *) {
                granted = try await store.requestFullAccessToEvents()
            } else {
                granted = try await store.requestAccess(to: .event)
            }
            guard granted else {
                emit(["status": "denied"])
                return
            }
            let calendars = store.calendars(for: .event)
            guard !calendars.isEmpty else {
                emit(["status": "no_calendars"])
                return
            }
            let predicate = store.predicateForEvents(withStart: start, end: end, calendars: calendars)
            let formatter = ISO8601DateFormatter()
            formatter.timeZone = .current
            // EventKit expands recurring events; skip cancelled or declined occurrences.
            let events = store.events(matching: predicate).filter { event in
                event.status != .canceled && !(event.attendees ?? []).contains {
                    $0.isCurrentUser && $0.participantStatus == .declined
                }
            }.sorted { $0.startDate < $1.startDate }
            let rows: [[String: Any]] = events.enumerated().map { index, event in
                ["id": index, "title": event.title ?? "",
                 "start": formatter.string(from: event.startDate),
                 "end": formatter.string(from: event.endDate),
                 "all_day": event.isAllDay, "calendar": event.calendar.title,
                 "location": event.location ?? "", "notes": event.notes ?? "",
                 "url": event.url?.absoluteString ?? ""]
            }
            emit(["status": "available", "timezone": TimeZone.current.identifier, "events": rows])
        } catch {
            let status = EKEventStore.authorizationStatus(for: .event)
            emit(["status": status == .denied || status == .restricted ? "denied" : "error"])
        }
    }
}
