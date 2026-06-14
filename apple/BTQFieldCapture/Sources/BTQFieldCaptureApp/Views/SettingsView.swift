import SwiftUI

struct SettingsView: View {
    @Bindable var model: FieldCaptureModel
    @Binding var screenMode: ScreenMode
    var onConnected: () -> Void = {}
    @State private var tokenOrLink = ""
    @State private var showingRemoveAccountConfirmation = false
    @FocusState private var isTokenInputFocused: Bool

    init(
        model: FieldCaptureModel,
        screenMode: Binding<ScreenMode> = .constant(.system),
        onConnected: @escaping () -> Void = {}
    ) {
        self.model = model
        _screenMode = screenMode
        self.onConnected = onConnected
    }

    var body: some View {
        Form {
            Section("Screen mode") {
                Picker("Screen mode", selection: $screenMode) {
                    ForEach(ScreenMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("settings.screen.mode")
            }

            Section("Account") {
                LabeledContent("Server", value: model.account.baseURL.absoluteString)
                LabeledContent("Person", value: model.session?.person.name ?? "Not connected")
                LabeledContent("Token", value: model.session?.token.label ?? "None")
            }

            if model.accounts.count > 1 {
                Section("Accounts") {
                    ForEach(model.accounts) { account in
                        accountSwitchButton(for: account)
                    }
                }
            }

            Section("Connect") {
                tokenInputField
                Button {
                    Task { await connect() }
                } label: {
                    Label(connectButtonLabel, systemImage: model.isConnecting ? "hourglass" : "key")
                }
                .disabled(model.isConnecting || tokenOrLink.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }

            Section("Sync") {
                LabeledContent("Status", value: model.statusMessage)
                Button {
                    Task { await model.syncPending() }
                } label: {
                    Label("Sync Pending Captures", systemImage: "arrow.trianglehead.2.clockwise")
                }
                .disabled(model.isSyncing || !model.canSubmitCaptures)
            }

            Section("Notifications") {
                LabeledContent("Sync Alerts", value: model.notificationPermissionStatus.displayName)
                    .accessibilityIdentifier("settings.notifications.status")
                Button {
                    Task { await model.requestNotificationPermission() }
                } label: {
                    Label(notificationButtonLabel, systemImage: "bell.badge")
                }
                .disabled(model.notificationPermissionStatus.allowsScheduling)
                .accessibilityIdentifier("settings.notifications.enable")
                Button {
                    Task { await model.sendTestNotification() }
                } label: {
                    Label("Send Test Alert", systemImage: "bell.and.waves.left.and.right")
                }
                .disabled(!model.notificationPermissionStatus.allowsScheduling)
                .accessibilityIdentifier("settings.notifications.test")
                Button {
                    Task { await model.sendTestUploadFailureNotification() }
                } label: {
                    Label("Send Failure Alert", systemImage: "exclamationmark.triangle")
                }
                .disabled(!model.notificationPermissionStatus.allowsScheduling)
                .accessibilityIdentifier("settings.notifications.test.failure")
                if model.notificationPermissionStatus == .denied {
                    Text("Enable notifications in system Settings to receive sync alerts.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Account Cleanup") {
                Button(role: .destructive) {
                    showingRemoveAccountConfirmation = true
                } label: {
                    Label("Remove This Account", systemImage: "trash")
                }
                .disabled(model.isSyncing || model.isConnecting)
                Text("Removes this cached account, its local workspace, and its stored token from this device.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
        #if os(iOS)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") {
                    isTokenInputFocused = false
                }
            }
        }
        #endif
        .task {
            await model.refreshNotificationPermissionStatus()
        }
        .confirmationDialog(
            "Remove this account?",
            isPresented: $showingRemoveAccountConfirmation,
            titleVisibility: .visible
        ) {
            Button("Remove Account", role: .destructive) {
                Task { await model.removeAccount(model.account.id) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This deletes the cached workspace and stored token for this account on this device.")
        }
    }

    private var tokenInputField: some View {
        #if os(iOS)
        TextField("Token or onboarding link", text: $tokenOrLink)
            .focused($isTokenInputFocused)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .privacySensitive()
            .textFieldStyle(.roundedBorder)
        #else
        TextField("Token or onboarding link", text: $tokenOrLink)
            .autocorrectionDisabled()
            .privacySensitive()
            .textFieldStyle(.roundedBorder)
        #endif
    }

    private func accountSwitchButton(for account: BTQAccount) -> some View {
        Button {
            Task { await model.switchAccount(account.id) }
        } label: {
            AccountSwitchRow(
                title: account.personName ?? account.label,
                subtitle: account.baseURL.host() ?? account.baseURL.absoluteString,
                isSelected: account.id == model.account.id
            )
        }
        .disabled(model.isSyncing || model.isConnecting || account.id == model.account.id)
    }

    private var notificationButtonLabel: String {
        switch model.notificationPermissionStatus {
        case .denied:
            "Check Sync Alert Permission"
        case .authorized, .provisional, .ephemeral:
            "Sync Alerts Enabled"
        case .notDetermined, .unknown:
            "Enable Sync Alerts"
        }
    }

    private var connectButtonLabel: String {
        model.isConnecting ? "Connecting" : "Connect"
    }

    private func connect() async {
        let value = tokenOrLink.trimmingCharacters(in: .whitespacesAndNewlines)
        isTokenInputFocused = false
        let didConnect: Bool
        if let url = URL(string: value), url.scheme != nil {
            didConnect = await model.connectWithOnboardingURL(url)
        } else {
            didConnect = await model.connect(token: value)
        }
        if didConnect {
            tokenOrLink = ""
            onConnected()
        }
    }
}

private struct AccountSwitchRow: View {
    let title: String
    let subtitle: String
    let isSelected: Bool

    var body: some View {
        HStack {
            VStack(alignment: .leading) {
                Text(title)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if isSelected {
                Image(systemName: "checkmark")
                    .foregroundStyle(Color.btqAccent)
            }
        }
    }
}
