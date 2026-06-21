import SwiftUI

struct InboxView: View {
    @Bindable var model: FieldCaptureModel

    var body: some View {
        List {
            statusSection
            inboxSection
            versionSection
        }
        .navigationTitle("Approval Inbox")
        .toolbar {
            Button {
                Task { await model.refreshInbox() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(model.isRefreshingInbox || !model.canReviewInbox)
            .accessibilityIdentifier("inbox.refresh")
        }
        .task {
            if model.canReviewInbox && model.inboxItems.isEmpty && !model.isRefreshingInbox {
                await model.refreshInbox()
            }
        }
    }

    private var statusSection: some View {
        Section {
            HStack {
                Label("\(model.inboxBadgeCount)", systemImage: "tray.full")
                    .font(.headline)
                    .foregroundStyle(model.inboxBadgeCount > 0 ? Color.btqAccent : Color.secondary)
                Text(model.inboxBadgeCount == 1 ? "item waiting" : "items waiting")
                    .foregroundStyle(.secondary)
                Spacer()
                if model.isRefreshingInbox || model.isReviewingInboxItem {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            .accessibilityIdentifier("inbox.summary")
        }
    }

    private var inboxSection: some View {
        Section("Pending Approval") {
            if !model.canReviewInbox {
                Text("This account does not have approval access.")
                    .foregroundStyle(.secondary)
            } else if model.inboxGroups.isEmpty {
                Text(model.isRefreshingInbox ? "Loading approvals..." : "Nothing waiting for approval.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(model.inboxGroups) { group in
                    if group.items.count > 1 {
                        InboxGroupRow(model: model, group: group)
                            .accessibilityIdentifier("inbox.group.\(group.id)")
                    } else if let item = group.items.first {
                        InboxItemRow(model: model, item: item)
                            .accessibilityIdentifier("inbox.item.\(item.draftID)")
                    }
                }
            }
        }
    }

    private var versionSection: some View {
        Section {
            AppVersionFooter()
                .listRowBackground(Color.clear)
        }
    }
}

private struct InboxItemRow: View {
    @Bindable var model: FieldCaptureModel
    let item: InboxItem
    @State private var isExpanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            InboxItemDetail(item: item)
                .padding(.top, 6)
            HStack {
                Button(role: .destructive) {
                    Task { await model.reviewInboxItem(item, action: .reject) }
                } label: {
                    Label("Reject", systemImage: "xmark")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(model.isReviewingInboxItem || model.isOfflineMode)

                Spacer()

                Button {
                    Task { await model.reviewInboxItem(item, action: .approve) }
                } label: {
                    Label("Approve", systemImage: "checkmark")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(model.isReviewingInboxItem || model.isOfflineMode)
            }
            .padding(.top, 6)
        } label: {
            InboxItemSummary(item: item)
        }
    }
}

private struct InboxGroupRow: View {
    @Bindable var model: FieldCaptureModel
    let group: InboxGroup
    @State private var approvedDraftIDs: Set<String>

    init(model: FieldCaptureModel, group: InboxGroup) {
        self.model = model
        self.group = group
        _approvedDraftIDs = State(initialValue: Set(group.items.map(\.draftID)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            InboxItemSummary(item: group.items[0], titleOverride: "Review job set")

            ForEach(group.items) { item in
                Button {
                    if approvedDraftIDs.contains(item.draftID) {
                        approvedDraftIDs.remove(item.draftID)
                    } else {
                        approvedDraftIDs.insert(item.draftID)
                    }
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: approvedDraftIDs.contains(item.draftID) ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(approvedDraftIDs.contains(item.draftID) ? Color.btqAccent : Color.secondary)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(displayJobType(item.jobType))
                                .font(.subheadline.weight(.semibold))
                            if !item.message.isEmpty {
                                Text(item.message)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        Spacer()
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Include \(displayJobType(item.jobType))")
                .accessibilityValue(approvedDraftIDs.contains(item.draftID) ? "Included" : "Rejected")
            }

            DisclosureGroup("Payloads") {
                ForEach(group.items) { item in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(displayJobType(item.jobType))
                            .font(.caption.weight(.bold))
                        PayloadRows(payload: item.payload)
                    }
                    .padding(.vertical, 4)
                }
            }
            .font(.caption)

            HStack {
                Spacer()
                Button {
                    Task { await model.reviewInboxSet(group, approvedDraftIDs: approvedDraftIDs) }
                } label: {
                    Label("Approve Set", systemImage: "checklist.checked")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(model.isReviewingInboxItem || model.isOfflineMode)
            }
        }
        .padding(.vertical, 4)
    }

}

private struct InboxItemSummary: View {
    let item: InboxItem
    var titleOverride: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label(sourceLabel(item.source), systemImage: sourceIcon(item.source))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if !item.site.isEmpty {
                    Text(item.site)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Text(titleOverride ?? displayJobType(item.jobType))
                .font(.headline)

            if !item.submitterName.isEmpty {
                Text(item.submitterName)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            if !item.message.isEmpty {
                Text(item.message)
                    .font(.callout)
                    .lineLimit(3)
            }

            if !item.createdAt.isEmpty {
                Text(item.createdAt)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct InboxItemDetail: View {
    let item: InboxItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !item.evidence.isEmpty {
                Text(item.evidence)
                    .font(.callout)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.btqAccent.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("Evidence: \(item.evidence)")
            }

            DisclosureGroup("Job payload") {
                PayloadRows(payload: item.payload)
            }
            .font(.caption)

            if !item.sourceCaptureID.isEmpty {
                LabeledContent("Capture", value: item.sourceCaptureID)
                    .font(.caption)
            }
            LabeledContent("Draft", value: item.draftID)
                .font(.caption)
        }
    }
}

private struct PayloadRows: View {
    let payload: [String: JSONValue]

    var body: some View {
        if payload.isEmpty {
            Text("No payload fields.")
                .foregroundStyle(.secondary)
        } else {
            ForEach(payload.keys.sorted(), id: \.self) { key in
                LabeledContent(key, value: payload[key]?.description ?? "")
                    .font(.caption)
            }
        }
    }
}

private func displayJobType(_ value: String) -> String {
    let text = value
        .replacingOccurrences(of: "_", with: " ")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return "Job draft" }
    return text
        .split(separator: " ")
        .map { word in
            word.prefix(1).uppercased() + word.dropFirst()
        }
        .joined(separator: " ")
}

private func sourceLabel(_ value: String) -> String {
    switch value.lowercased() {
    case "voice": "Voice"
    case "photo": "Photo"
    case "text": "Text"
    default: "Note"
    }
}

private func sourceIcon(_ value: String) -> String {
    switch value.lowercased() {
    case "voice": "waveform"
    case "photo": "camera"
    case "text": "text.alignleft"
    default: "note.text"
    }
}
