from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout

class QueueManager:
    def __init__(self):
        self.queues = {
            "scrape": [],
            "process": [],
            "cache": [],
            "verify": []
        }
        self.item_states = {
            "wanted": 0,
            "in_queue": 0,
            "added": 0,
            "completed": 0
        }

    async def add_to_queue(self, queue_name, item):
        self.queues[queue_name].append(item)
        self.item_states["in_queue"] += 1

    async def process_queues(self):
        # Process items in queues
        pass

    def update_item_state(self, old_state, new_state):
        self.item_states[old_state] -= 1
        self.item_states[new_state] += 1

    def display_status(self):
        layout = Layout()
        layout.split_column(
            Layout(name="upper"),
            Layout(name="lower")
        )

        queue_table = Table(title="Queue Status")
        queue_table.add_column("Queue", style="cyan")
        queue_table.add_column("Items", style="magenta")

        for queue_name, items in self.queues.items():
            queue_table.add_row(queue_name, str(len(items)))

        state_table = Table(title="Item States")
        state_table.add_column("State", style="cyan")
        state_table.add_column("Count", style="magenta")

        for state, count in self.item_states.items():
            state_table.add_row(state, str(count))

        layout["upper"].update(Panel(queue_table))
        layout["lower"].update(Panel(state_table))

        return layout

    async def run_display(self):
        with Live(self.display_status(), refresh_per_second=4) as live:
            while True:
                live.update(self.display_status())
                await asyncio.sleep(0.25)