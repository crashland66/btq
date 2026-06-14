import Foundation

public extension BTQSession {
    static let demo = BTQSession(
        person: BTQPerson(personID: "demo.employee", name: "Demo Employee"),
        token: BTQToken(tokenID: "demo-token", label: "Demo Field Token"),
        sites: [
            BTQSite(
                siteID: "site_dickinson",
                label: "Dickinson Center",
                captureGuidance: "Capture reality quickly: photos first, voice if details matter.",
                displayCategories: [
                    BTQDisplayCategory(value: "cleaning_quality", label: "Cleaning quality"),
                    BTQDisplayCategory(value: "staffing", label: "Staffing"),
                    BTQDisplayCategory(value: "supplies", label: "Supplies"),
                    BTQDisplayCategory(value: "incident", label: "Incident"),
                ],
                isFavorite: true,
                lastUsedAt: .now
            ),
            BTQSite(
                siteID: "site_main",
                label: "Main Office",
                captureGuidance: "Note visible conditions and anything requiring operator follow-up.",
                displayCategories: [
                    BTQDisplayCategory(value: "general_note", label: "General note"),
                    BTQDisplayCategory(value: "supplies", label: "Supplies"),
                ]
            ),
        ],
        canSubmit: true,
        canReview: false,
        maxImages: 6
    )
}
