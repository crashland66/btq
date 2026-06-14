import SwiftUI

struct SitesView: View {
    @Bindable var model: FieldCaptureModel
    var onSiteSelected: () -> Void = {}
    @State private var searchText = ""

    private var filteredSites: [BTQSite] {
        guard !searchText.isEmpty else { return model.prioritizedSites }
        return model.prioritizedSites.filter {
            $0.label.localizedCaseInsensitiveContains(searchText)
                || $0.siteID.localizedCaseInsensitiveContains(searchText)
        }
    }

    var body: some View {
        List(filteredSites) { site in
            HStack {
                Button {
                    model.selectedSite = site
                    onSiteSelected()
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(site.label)
                                .foregroundStyle(.primary)
                            Text(site.siteID)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if site.siteID == model.selectedSiteID {
                            Label("Selected", systemImage: "checkmark.circle.fill")
                                .labelStyle(.iconOnly)
                                .foregroundStyle(.tint)
                        }
                    }
                }
                .buttonStyle(.plain)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
                .accessibilityLabel("Select site \(site.label)")
                .accessibilityValue(site.siteID == model.selectedSiteID ? "Selected" : "Not selected")
                .accessibilityHint("Switches field capture to this site and returns to Capture.")

                Button {
                    Task { await model.toggleFavorite(site: site) }
                } label: {
                    Image(systemName: site.isFavorite ? "star.fill" : "star")
                }
                .buttonStyle(.plain)
                .accessibilityLabel(site.isFavorite ? "Remove \(site.label) from favorites" : "Add \(site.label) to favorites")
                .accessibilityHint("Favorites appear first in the site list.")
            }
        }
        .searchable(text: $searchText)
        .navigationTitle("Sites")
    }
}
