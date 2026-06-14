import Foundation
import Security

public protocol SecureTokenStore: Sendable {
    func saveToken(_ token: String, accountID: UUID) async throws
    func loadToken(accountID: UUID) async throws -> String?
    func deleteToken(accountID: UUID) async throws
}

public enum KeychainTokenStoreError: Error, Equatable {
    case unexpectedStatus(OSStatus)
}

public final class KeychainTokenStore: SecureTokenStore, @unchecked Sendable {
    private let service: String

    public init(service: String = "com.btq.fieldcapture.token") {
        self.service = service
    }

    public func saveToken(_ token: String, accountID: UUID) async throws {
        let data = Data(token.utf8)
        let account = accountID.uuidString
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]

        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            return
        }
        if updateStatus != errSecItemNotFound {
            throw KeychainTokenStoreError.unexpectedStatus(updateStatus)
        }

        var addQuery = query
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw KeychainTokenStoreError.unexpectedStatus(addStatus)
        }
    }

    public func loadToken(accountID: UUID) async throws -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountID.uuidString,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainTokenStoreError.unexpectedStatus(status)
        }
        guard let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    public func deleteToken(accountID: UUID) async throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountID.uuidString,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainTokenStoreError.unexpectedStatus(status)
        }
    }
}

public actor MemoryTokenStore: SecureTokenStore {
    private var tokens: [UUID: String] = [:]

    public init() {}

    public func saveToken(_ token: String, accountID: UUID) async {
        tokens[accountID] = token
    }

    public func loadToken(accountID: UUID) async -> String? {
        tokens[accountID]
    }

    public func deleteToken(accountID: UUID) async {
        tokens.removeValue(forKey: accountID)
    }
}
