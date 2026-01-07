from nuts import NutsJob


class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'LoadData'

    def run(self, **kwargs):
        """Load transformed data to destination."""
        try:
            transform_results = kwargs.get('transform_results', {})

            # Simulate loading
            self.result = {
                'records_loaded': transform_results.get('records_transformed', 1000),
                'destination': 'warehouse.example.com'
            }
            self.success = True
        except Exception as e:
            self.error = e
            self.success = False
