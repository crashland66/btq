import SwiftUI

struct InitialSetupView: View {
    @Bindable var model: FieldCaptureModel
    var onConnected: () -> Void = {}
    @State private var tokenOrLink = ""
    @FocusState private var isTokenInputFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                FieldCaptureBrandHeader()

                VStack(alignment: .leading, spacing: 8) {
                    Text("Set Up Field Capture")
                        .font(.title.bold())
                    Text("Paste the setup token from your TestFlight notes.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                VStack(alignment: .leading, spacing: 12) {
                    tokenInputField

                    Button {
                        Task { await connect() }
                    } label: {
                        Label(connectButtonLabel, systemImage: model.isConnecting ? "hourglass" : "key")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(model.isConnecting || tokenOrLink.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("initial.setup.connect")

                    Text(model.statusMessage)
                        .font(.footnote)
                        .foregroundStyle(statusColor)
                        .accessibilityIdentifier("initial.setup.status")
                }
                .padding(16)
                .background(.background)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
                }

                AppVersionFooter()
            }
            .padding(.horizontal, 20)
            .padding(.top, 32)
            .padding(.bottom, 32)
            .frame(maxWidth: 520, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .navigationBar)
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
            guard model.hasLoaded && model.needsInitialSetup else { return }
            isTokenInputFocused = true
        }
    }

    private var tokenInputField: some View {
        #if os(iOS)
        TextField("Paste setup token", text: $tokenOrLink)
            .focused($isTokenInputFocused)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .privacySensitive()
            .textFieldStyle(.roundedBorder)
            .accessibilityIdentifier("initial.setup.token")
        #else
        TextField("Paste setup token", text: $tokenOrLink)
            .focused($isTokenInputFocused)
            .autocorrectionDisabled()
            .privacySensitive()
            .textFieldStyle(.roundedBorder)
            .accessibilityIdentifier("initial.setup.token")
        #endif
    }

    private var connectButtonLabel: String {
        model.isConnecting ? "Connecting" : "Connect"
    }

    private var statusColor: Color {
        model.requiresReconnect ? .red : .secondary
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
