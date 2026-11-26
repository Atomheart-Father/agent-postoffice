import CryptoKit
import Darwin
import Foundation

/// 本机未决写：只保存冻结意图，不保存端点令牌；结果未知时由使用者决定重试。
public struct ActionJournal {
    private struct Entry: Codable {
        var intent: Intent
        var subject: String
        var text: String
        var need: String
        var actKey: String
        var retryRef: String?
        init(_ a: ActionState) {
            intent = a.intent; subject = a.subject; text = a.text; need = a.need
            actKey = a.actKey; retryRef = a.retryRef
        }
        var action: ActionState {
            var a = ActionState(intent: intent, subject: subject, text: text, need: need, actKey: actKey,
                                retryRef: retryRef, phase: .unknown(detail: tr("上次发送结果未知", "Previous result unknown")))
            a.mayHaveCommitted = true
            return a
        }
    }
    private let directory: URL
    private let file: URL
    public init(home: URL, actor: String, endpointIdentity: String) {
        directory = home.appendingPathComponent("app/unresolved", isDirectory: true)
        let digest = SHA256.hash(data: Data((actor + "\0" + endpointIdentity).utf8)).map { String(format: "%02x", $0) }.joined()
        file = directory.appendingPathComponent(digest + ".json")
    }
    private func locked<T>(_ body: () throws -> T) throws -> T {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let fd = open(file.path + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFREG,
              info.st_mode & 0o077 == 0, flock(fd, LOCK_EX) == 0 else { throw POSIXError(.EACCES) }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }
    private func read() throws -> [String: ActionState] {
        let fd = open(file.path, O_RDONLY | O_NOFOLLOW)
        if fd < 0 {
            if errno == ENOENT { return [:] }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFREG,
              info.st_mode & 0o077 == 0 else { close(fd); throw POSIXError(.EACCES) }
        let data = try FileHandle(fileDescriptor: fd, closeOnDealloc: true).readToEnd() ?? Data()
        return try JSONDecoder().decode([String: Entry].self, from: data).mapValues(\.action)
    }
    public func load() throws -> [String: ActionState] { try locked { try read() } }

    private func sameIntent(_ a: ActionState, _ b: ActionState) -> Bool {
        a.actKey == b.actKey && a.intent == b.intent && a.subject == b.subject && a.text == b.text && a.need == b.need
    }

    /// 原子认领动作；别的窗口已有同入口未决意图时，返回其冻结内容，调用方不得发送新意图。
    public func claim(_ key: String, action: ActionState) throws -> ActionState? {
        try locked {
            var current = try read()
            if let existing = current[key] {
                guard sameIntent(existing, action), existing.retryRef == action.retryRef else { return existing }
                return nil
            }
            current[key] = action
            try write(current)
            return nil
        }
    }

    /// 只更新当前仍属于原键的记录；迟到结果不能删除或复活其他窗口的动作。
    public func finish(_ key: String, expected: ActionState, pending: ActionState?) throws {
        try locked {
            var current = try read()
            guard let existing = current[key], sameIntent(existing, expected) else { return }
            if var pending {
                if existing.retryRef != expected.retryRef && pending.retryRef == expected.retryRef { pending.retryRef = existing.retryRef }
                current[key] = pending
            } else { current.removeValue(forKey: key) }
            try write(current)
        }
    }

    public enum Attempt {
        case busy
        case existing(ActionState)
        case completed(WriteOutcome, persisted: Bool)
    }

    /// 同入口只允许一个进程持有在途尝试；进程退出自动释放，不阻塞其他入口。
    public func attempt(_ key: String, action: ActionState, send: () -> WriteOutcome) throws -> Attempt {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let digest = SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
        let fd = open(file.path + "." + digest + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFREG,
              info.st_mode & 0o077 == 0 else { throw POSIXError(.EACCES) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            if errno == EWOULDBLOCK || errno == EAGAIN { return .busy }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer { flock(fd, LOCK_UN) }
        if let existing = try claim(key, action: action) { return .existing(existing) }
        let outcome = send()
        var book = ActionBook()
        book.actions[key] = action
        book.apply(outcome, to: key)
        do { try finish(key, expected: action, pending: book.unresolved[key]); return .completed(outcome, persisted: true) }
        catch { return .completed(outcome, persisted: false) }
    }

    private func write(_ actions: [String: ActionState]) throws {
        let data = try JSONEncoder().encode(actions.mapValues(Entry.init))
        let temporary = directory.appendingPathComponent("." + UUID().uuidString)
        let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd); try? FileManager.default.removeItem(at: temporary) }
        try data.withUnsafeBytes { bytes in
            var sent = 0
            while sent < bytes.count {
                let n = Darwin.write(fd, bytes.baseAddress!.advanced(by: sent), bytes.count - sent)
                guard n > 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
                sent += n
            }
        }
        guard fsync(fd) == 0, rename(temporary.path, file.path) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    }
}
