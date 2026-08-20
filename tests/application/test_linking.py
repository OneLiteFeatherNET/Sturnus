from sturnus.application.linking import new_state


def test_states_are_unguessable_and_distinct() -> None:
    states = {new_state() for _ in range(200)}
    assert len(states) == 200
    assert all(len(s) >= 32 for s in states)
