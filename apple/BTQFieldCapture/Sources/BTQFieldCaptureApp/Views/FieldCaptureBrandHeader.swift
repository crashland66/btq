import SwiftUI

struct FieldCaptureBrandHeader: View {
    var body: some View {
        HStack {
            Image("FieldCaptureHeader", bundle: .module)
                .resizable()
                .scaledToFit()
                .frame(width: 276, height: 56, alignment: .leading)
                .accessibilityLabel("Field Capture")
                .accessibilityIdentifier("brand.field-capture.header")
            Spacer(minLength: 0)
        }
    }
}
