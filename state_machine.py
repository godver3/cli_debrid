
class StateMachine:
    def __init__(self):
        self.state = "initial"

    def transition(self, new_state):
        # Add your state transition logic here
        self.state = new_state
