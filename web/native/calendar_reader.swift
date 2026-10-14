import EventKit
import Foundation

// Read calendar occurrences and change one event in the selected calendar.
@main
struct CalendarReader {
    static let markerPrefix = "Social Crawler ID: "
    static let duplicateWindowSeconds: TimeInterval = 86_400
    static let startMatchSeconds: TimeInterval = 60

    struct Candidate {
        let id: String
        let title: String
        let start: Date
        let url: String
    }

    static func emit(_ data: [String: Any]) {
        if let json = try? JSONSerialization.data(withJSONObject: data),
           let text = String(data: json, encoding: .utf8) {
            print(text)
        }
    }

    static func main() async {
        if CommandLine.arguments.count == 2 {
            switch CommandLine.arguments[1] {
            case "list":
                await listCalendars()
            case "check":
                checkCalendar()
            case "status":
                calendarStatus()
            case "save":
                await saveEvent()
            case "remove":
                await removeEvent()
            default:
                emit(["status": "invalid_request"])
            }
            return
        }
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
            let granted = try await requestAccess(store)
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

    static func requestAccess(_ store: EKEventStore) async throws -> Bool {
        if #available(macOS 14.0, *) {
            return try await store.requestFullAccessToEvents()
        }
        return try await store.requestAccess(to: .event)
    }

    static func writableCalendars(_ store: EKEventStore) -> [EKCalendar] {
        store.calendars(for: .event).filter { $0.allowsContentModifications }
    }

    static func calendarLabel(_ calendar: EKCalendar) -> String {
        "\(calendar.source?.title ?? "")/\(calendar.title)"
    }

    static func listCalendars() async {
        let store = EKEventStore()
        do {
            guard try await requestAccess(store) else {
                emit(["status": "denied"])
                return
            }
            let rows = writableCalendars(store).map { calendar in
                ["account": calendar.source?.title ?? "", "calendar": calendar.title,
                 "selector": calendarLabel(calendar)]
            }
            emit(["status": "available", "calendars": rows])
        } catch {
            emit(["status": "error"])
        }
    }

    static func selectedCalendar(_ store: EKEventStore, name: String) -> (EKCalendar?, String) {
        // Require the full account and calendar name to avoid a wrong account.
        let calendars = writableCalendars(store)
        guard !calendars.isEmpty else { return (nil, "no_calendars") }
        let qualified = calendars.filter { calendarLabel($0) == name }
        if qualified.count > 1 { return (nil, "calendar_ambiguous") }
        return qualified.count == 1 ? (qualified[0], "available") : (nil, "calendar_not_found")
    }

    static func checkCalendar() {
        // Check access without a permission prompt or a calendar write.
        guard let input = try? FileHandle.standardInput.readToEnd(),
              let payload = try? JSONSerialization.jsonObject(with: input) as? [String: String],
              let target = payload["calendar"], !target.isEmpty else {
            emit(["status": "invalid_request"])
            return
        }
        if let status = accessStatus() {
            emit(["status": status])
            return
        }
        let (calendar, status) = selectedCalendar(EKEventStore(), name: target)
        emit(["status": status, "selector": calendar.map(calendarLabel) ?? ""])
    }

    static func accessStatus() -> String? {
        let access = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) {
            if access == .fullAccess { return nil }
        } else {
            if access == .authorized { return nil }
        }
        return access == .notDetermined ? "permission_needed" : "denied"
    }

    static func matches(_ event: EKEvent, candidate: Candidate) -> Bool {
        let marker = markerPrefix + candidate.id
        if abs(event.startDate.timeIntervalSince(candidate.start)) <= duplicateWindowSeconds,
           (event.notes ?? "").components(separatedBy: .newlines).contains(marker) {
            return true
        }
        guard abs(event.startDate.timeIntervalSince(candidate.start)) <= startMatchSeconds else {
            return false
        }
        let sameTitle = (event.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            .localizedCaseInsensitiveCompare(candidate.title.trimmingCharacters(in: .whitespacesAndNewlines))
            == .orderedSame
        let sameURL = !candidate.url.isEmpty && (
            event.url?.absoluteString == candidate.url ||
            (event.notes ?? "").contains("Original event: \(candidate.url)"))
        return sameTitle || sameURL
    }

    static func calendarStatus() {
        guard let input = try? FileHandle.standardInput.readToEnd(),
              let payload = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
              let target = payload["calendar"] as? String, !target.isEmpty,
              let rows = payload["events"] as? [[String: String]] else {
            emit(["status": "invalid_request"])
            return
        }
        if let status = accessStatus() {
            emit(["status": status])
            return
        }
        let store = EKEventStore()
        let (calendar, status) = selectedCalendar(store, name: target)
        guard let calendar else {
            emit(["status": status])
            return
        }
        let candidates = rows.compactMap { row -> Candidate? in
            guard let id = row["id"], let title = row["title"],
                  let start = row["start"].flatMap(parseDate) else { return nil }
            return Candidate(id: id, title: title, start: start, url: row["url"] ?? "")
        }
        // Read each month once, even when the list shows many events.
        let monthFormatter = DateFormatter()
        monthFormatter.calendar = Calendar.current
        monthFormatter.dateFormat = "yyyy-MM"
        let groups = Dictionary(grouping: candidates) { candidate in
            monthFormatter.string(from: candidate.start)
        }
        var present = Set<String>()
        var ambiguous = Set<String>()
        for group in groups.values {
            guard let first = group.first,
                  let month = Calendar.current.dateInterval(of: .month, for: first.start) else {
                continue
            }
            let predicate = store.predicateForEvents(
                withStart: month.start.addingTimeInterval(-duplicateWindowSeconds),
                end: month.end.addingTimeInterval(duplicateWindowSeconds), calendars: [calendar])
            let calendarEvents = store.events(matching: predicate).filter { $0.status != .canceled }
            for candidate in group {
                let count = calendarEvents.filter { matches($0, candidate: candidate) }.count
                if count > 0 { present.insert(candidate.id) }
                if count > 1 { ambiguous.insert(candidate.id) }
            }
        }
        emit(["status": "available", "selector": calendarLabel(calendar),
              "present_ids": present.sorted(), "ambiguous_ids": ambiguous.sorted()])
    }

    static func parseDate(_ value: String) -> Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        if let date = parser.date(from: value) { return date }
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return parser.date(from: value)
    }

    static func saveEvent() async {
        guard let input = try? FileHandle.standardInput.readToEnd(),
              let payload = try? JSONSerialization.jsonObject(with: input) as? [String: String],
              let target = payload["calendar"], !target.isEmpty,
              let id = payload["id"], !id.isEmpty,
              let title = payload["title"], !title.isEmpty,
              let start = payload["start"].flatMap(parseDate),
              let end = payload["end"].flatMap(parseDate), end > start else {
            emit(["status": "invalid_event"])
            return
        }
        let store = EKEventStore()
        do {
            guard try await requestAccess(store) else {
                emit(["status": "denied"])
                return
            }
            let (calendar, status) = selectedCalendar(store, name: target)
            guard let calendar else {
                emit(["status": status])
                return
            }
            let candidate = Candidate(id: id, title: title, start: start,
                                      url: payload["url"] ?? "")
            let nearby = store.predicateForEvents(
                withStart: start.addingTimeInterval(-duplicateWindowSeconds),
                end: end.addingTimeInterval(duplicateWindowSeconds), calendars: [calendar])
            if store.events(matching: nearby).contains(where: {
                $0.status != .canceled && matches($0, candidate: candidate)
            }) {
                emit(["status": "already_added"])
                return
            }
            let event = EKEvent(eventStore: store)
            event.calendar = calendar
            event.title = title
            event.startDate = start
            event.endDate = end
            event.location = payload["location"] ?? ""
            let description = payload["description"] ?? ""
            let url = payload["url"] ?? ""
            event.notes = [description, url.isEmpty ? "" : "Original event: \(url)",
                           markerPrefix + id]
                .filter { !$0.isEmpty }.joined(separator: "\n\n")
            if let sourceURL = URL(string: url), sourceURL.scheme == "https" || sourceURL.scheme == "http" {
                event.url = sourceURL
            }
            try store.save(event, span: .thisEvent, commit: true)
            emit(["status": "saved"])
        } catch {
            emit(["status": "error"])
        }
    }

    static func removeEvent() async {
        guard let input = try? FileHandle.standardInput.readToEnd(),
              let payload = try? JSONSerialization.jsonObject(with: input) as? [String: String],
              let target = payload["calendar"], !target.isEmpty,
              let id = payload["id"], !id.isEmpty,
              let title = payload["title"], !title.isEmpty,
              let start = payload["start"].flatMap(parseDate) else {
            emit(["status": "invalid_event"])
            return
        }
        let store = EKEventStore()
        do {
            guard try await requestAccess(store) else {
                emit(["status": "denied"])
                return
            }
            let (calendar, status) = selectedCalendar(store, name: target)
            guard let calendar else {
                emit(["status": status])
                return
            }
            let candidate = Candidate(id: id, title: title, start: start,
                                      url: payload["url"] ?? "")
            // Limit removal to one matching occurrence in the selected calendar.
            let nearby = store.predicateForEvents(
                withStart: start.addingTimeInterval(-duplicateWindowSeconds),
                end: start.addingTimeInterval(duplicateWindowSeconds), calendars: [calendar])
            let matchingEvents = store.events(matching: nearby).filter {
                $0.status != .canceled && matches($0, candidate: candidate)
            }
            guard matchingEvents.count == 1 else {
                emit(["status": matchingEvents.isEmpty ? "not_found" : "multiple_matches"])
                return
            }
            try store.remove(matchingEvents[0], span: .thisEvent, commit: true)
            emit(["status": "removed"])
        } catch {
            emit(["status": "error"])
        }
    }
}
