import SwiftUI

struct ReconciliationFieldDiagnosticView: View {
    let records: [ReconciliationFieldDiagnosticRecord]

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                if records.isEmpty {
                    Text("No reconciliation runs recorded yet.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(records) { record in
                        Text(record.renderedText)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if record.id != records.last?.id {
                            Divider()
                        }
                    }
                }
            }
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
            .padding()
        }
        .navigationTitle("Upload Reconciliation")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
    }
}
