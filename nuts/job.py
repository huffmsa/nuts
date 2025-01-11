from typing import Union


class Job():
    '''
        A Nuts Job
    '''
    name: str
    schedule: Union[str, None]
    success: bool
    result: object
