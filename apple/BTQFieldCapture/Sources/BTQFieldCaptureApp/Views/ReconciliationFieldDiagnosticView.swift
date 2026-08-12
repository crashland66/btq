import SwiftUI

struct ReconciliationFieldDiagnosticView: View {
    let records: [ReconciliationFieldDiagnosticRecord]

    #if os(iOS)
    @State private var isConfirmingTermination = false
    #endif

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                #if os(iOS)
                terminationControl
                Divider()
                #endif

                if records.isEmpty {
                    Text("No reconciliation runs recorded yet.")
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(records) { record in
                        Text(record.renderedText)
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if record.id != records.last?.id {
                            Divider()
                        }
                    }
                }
            }
            .textSelection(.enabled)
            .padding()
        }
        .navigationTitle("Upload Reconciliation")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
    }

    #if os(iOS)
    private var terminationControl: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Simulate Abrupt App Termination")
                .font(.headline)
            Text(
                "Kills the app immediately without saving or cleanup so background upload recovery can be observed after relaunch."
            )
            .font(.subheadline)
            .foregroundStyle(.secondary)

            Button("Kill App for Upload Recovery Test", role: .destructive) {
                isConfirmingTermination = true
            }
            .buttonStyle(.bordered)
        }
        .confirmationDialog(
            "Kill the app immediately?",
            isPresented: $isConfirmingTermination,
            titleVisibility: .visible
        ) {
            Button("Kill App Immediately", role: .destructive) {
                ReconciliationFieldDiagnosticTermination.terminateImmediately()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "The app will crash now without saving or cleanup. Relaunch it to observe background upload recovery in Diagnostics."
            )
        }
    }
    #endif
}
