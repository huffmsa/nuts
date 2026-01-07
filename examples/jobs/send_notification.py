from nuts import NutsJob


class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'SendNotification'

    def run(self, **kwargs):
        """Send completion notification."""
        try:
            load_results = kwargs.get('load_results', {})

            # Simulate notification
            self.result = {
                'notification_sent': True,
                'message': f"Pipeline completed: {load_results.get('records_loaded', 0)} records loaded"
            }
            self.success = True
        except Exception as e:
            self.error = e
            self.success = False
