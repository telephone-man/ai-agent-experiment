from services.voice_gateway.turn_taking import SemanticTurnDetector, TurnAction


def test_backchannel_does_not_reach_llm():
    detector = SemanticTurnDetector()

    decision = detector.decide("mhmm", is_final=True, agent_is_speaking=True)

    assert decision.action == TurnAction.BACKCHANNEL
    assert decision.should_interrupt is False


def test_partial_long_speech_interrupts_playback():
    detector = SemanticTurnDetector()

    decision = detector.decide(
        "Actually I need to ask something else",
        is_final=False,
        agent_is_speaking=True,
    )

    assert decision.action == TurnAction.PARTIAL
    assert decision.should_interrupt is True


def test_goodbye_is_deterministic():
    detector = SemanticTurnDetector()

    decision = detector.decide("thanks goodbye", is_final=True)

    assert decision.action == TurnAction.GOODBYE
    assert decision.should_interrupt is True


def test_multilingual_backchannel_does_not_reach_translation():
    detector = SemanticTurnDetector()

    decision = detector.decide("oui", is_final=True)

    assert decision.action == TurnAction.BACKCHANNEL
    assert decision.should_interrupt is False


def test_multilingual_goodbye_is_deterministic():
    detector = SemanticTurnDetector()

    decision = detector.decide("au revoir", is_final=True)

    assert decision.action == TurnAction.GOODBYE
    assert decision.should_interrupt is True


def test_final_question_becomes_user_turn():
    detector = SemanticTurnDetector()

    decision = detector.decide("What is the status of my account?", is_final=True)

    assert decision.action == TurnAction.USER_TURN
