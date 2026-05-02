from services.voice_gateway.models import CallLeg, CallSession, LegRole, SessionMode


def test_call_session_supports_future_translation_legs():
    session = CallSession(session_id="call-1", mode=SessionMode.TRANSLATION)
    session.add_leg(
        CallLeg(
            leg_id="a",
            fs_uuid="uuid-a",
            role=LegRole.CALLER,
            aor="sip:alice@voice.local",
            source_language="fr",
            target_language="en",
            peer_leg_id="b",
        )
    )
    session.add_leg(
        CallLeg(
            leg_id="b",
            fs_uuid="uuid-b",
            role=LegRole.PEER,
            aor="sip:bob@voice.local",
            source_language="en",
            target_language="fr",
            peer_leg_id="a",
        )
    )

    assert session.primary_leg().fs_uuid == "uuid-a"
    assert session.legs["a"].target_language == "en"
    assert session.legs["b"].peer_leg_id == "a"
    assert session.leg_by_uuid("uuid-b").aor == "sip:bob@voice.local"
    assert session.peer_for(session.legs["a"]).fs_uuid == "uuid-b"
