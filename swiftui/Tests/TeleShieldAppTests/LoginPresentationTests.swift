import XCTest
@testable import TeleShieldApp

final class LoginPresentationTests: XCTestCase {
    func testSuccessfulAuthenticationForCurrentAccountDismissesSheet() {
        XCTAssertTrue(
            AuthenticationPresentation.shouldDismissLoginSheet(
                event: "auth_succeeded",
                accountID: "account-a",
                targetAccountID: "account-a"
            )
        )
    }

    func testAuthenticationFailureKeepsSheetOpen() {
        XCTAssertFalse(
            AuthenticationPresentation.shouldDismissLoginSheet(
                event: "auth_failed",
                accountID: nil,
                targetAccountID: "account-a"
            )
        )
    }

    func testSuccessfulAuthenticationForAnotherAccountDoesNotDismissSheet() {
        XCTAssertFalse(
            AuthenticationPresentation.shouldDismissLoginSheet(
                event: "auth_succeeded",
                accountID: "account-b",
                targetAccountID: "account-a"
            )
        )
    }
}
