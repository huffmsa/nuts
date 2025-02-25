from typing import Union, Any


class NutsJob():
    '''
        A Nuts Job
    '''
    name: str
    schedule: Union[str, None]
    success: bool
    result: Any
    error: Exception
    next: Union[str, None]

    def __init__(self):
        self.name = None
        self.schedule = None
        self.success = False
        self.result = None
        self.error = None
        self.next = None
