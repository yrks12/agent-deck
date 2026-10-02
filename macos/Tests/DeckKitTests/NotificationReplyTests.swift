import XCTest
@testable import DeckKit

/// **Reply from the notification, without opening the app.** A desk's message
/// to him banners with a Reply button that takes typed text
/// (`UNTextInputNotificationAction`); what he types is sent into that desk's
/// thread as a quote-reply to the message that buzzed.
///
/// The notification centre exists only inside an app bundle, so what is
/// pinned here is the pure half both apps share: which banners offer Reply,
/// and what a typed answer turns into.
final class NotificationReplyTests: XCTestCase {

    private func alert(_ json: String) throws -> OwnerAlert {
        try DeckCoding.decoder.decode(OwnerAlert.self, from: Data(json.utf8))
    }

    private var message: OwnerAlert {
        get throws {
            try alert(#"""
            {"id":"msg:a1","kind":"for_you","source":"message","agent":"atlas",
             "thread_id":"direct:atlas","card_id":"","message_id":"a1",
             "title":"Atlas","body":"Deployed. Tag it?","ts":1.0,"cursor":"1-a1"}
            """#)
        }
    }

    func testADesksMessageBannersWithAReplyThatQuotesIt() throws {
        let text = AlertNotificationText(try message)
        XCTAssertEqual(text.categoryIdentifier, NotificationReply.categoryIdentifier)
        XCTAssertEqual(text.userInfo["deck_message"], "a1")
    }

    func testAnApprovalOffersNoTypedReply() throws {
        let ask = try alert(#"""
        {"id":"ask:k7","kind":"needs_you","source":"approval","agent":"scout",
         "thread_id":"direct:scout","card_id":"k7","message_id":"","title":"Scout needs you",
         "body":"Run a command","ts":1.0,"cursor":"1-k7"}
        """#)
        XCTAssertNil(AlertNotificationText(ask).categoryIdentifier,
                     "an approval is answered by its own buttons, not by typing at it")
    }

    func testTheTypedAnswerBecomesAQuoteReplyInThatThread() throws {
        let info = AlertNotificationText(try message).userInfo
        let reply = NotificationReply(actionIdentifier: NotificationReply.actionIdentifier,
                                      userInfo: info, typed: "  yes, tag it \n")
        XCTAssertEqual(reply, NotificationReply(threadID: "direct:atlas", text: "yes, tag it", replyTo: "a1"))
    }

    func testATapOrAnEmptyAnswerSendsNothing() throws {
        let info = AlertNotificationText(try message).userInfo
        XCTAssertNil(NotificationReply(actionIdentifier: "com.apple.UNNotificationDefaultActionIdentifier",
                                       userInfo: info, typed: "yes"))
        XCTAssertNil(NotificationReply(actionIdentifier: NotificationReply.actionIdentifier,
                                       userInfo: info, typed: "   "))
    }
}
