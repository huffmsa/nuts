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

    def __init__(self, **kwargs):
        '''
            Initialize the NutsJob class.
        '''
        self.name = kwargs.get('name', None)
        self.schedule = kwargs.get('schedule', None)
        self.success = kwargs.get('success', False)
        self.result = kwargs.get('result', None)
        self.error = kwargs.get('error', None)
        self.next = kwargs.get('next', None)
