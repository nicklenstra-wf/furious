#
# Copyright 2012 WebFilings, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Retrieve app engine tasks from testbed queues and run them.

The purpose is to run full local integration tests with the App Engine testbed.

Advanced app engine features such as automatic retries are not implemented.


Examples:

# See integration test for more detailed taskq service setup.
taskq_service = testbed.get_stub(testbed.TASKQUEUE_SERVICE_NAME)


# Run all tasks in all queues until they are empty.
run(taskq_service)


# Run all tasks in all queues until empty or until 5 iterations is reached.
run(taskq_service, max_iterations=5)


# Run all tasks from selected queues until they are empty.
run(taskq_service, ["queue1", "queue2"])


# Setup state for running multiple times.
runner = Runner(taskq_service)
runner.run()
...
runner.run()

"""

import base64
from collections import defaultdict
import os
import random
import uuid

from google.appengine.api import taskqueue

from furious.context._local import _clear_context
from furious.handlers import process_async_task

__all__ = ['run', 'run_queue', 'Runner', 'add_tasks', 'get_tasks', 'purge_tasks',
           'get_queue_names', 'get_pull_queue_names', 'get_push_queue_names']


def run_queue(taskq_service, queue_name):
    """Get the tasks from a queue.  Clear the queue, and run each task.

    If tasks are reinserted into this queue, this function needs to be called
    again for them to run.
    """

    # Get tasks and clear them
    tasks = taskq_service.GetTasks(queue_name)

    taskq_service.FlushQueue(queue_name)

    num_processed = 0

    for task in tasks:
        _execute_task(task)

        num_processed += 1

    return num_processed


def run(taskq_service, queue_names=None, max_iterations=None):
    """
    Run all the tasks in queues, limited by max_iterations.

    An 'iteration' processes at least all current tasks in the queues.
    If any tasks insert additional tasks into a queue that has already
    been processed, at least one more iteration is needed.

    :param taskq_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queue_names: :class: `list` of queue name strings.
    :param max_iterations: :class: `int` maximum number of iterations to run.
    """

    if not queue_names:
        queue_names = get_push_queue_names(taskq_service)

    iterations = 0
    tasks_processed = 0
    processed = (max_iterations is None or max_iterations > 0)

    # Keep processing if we have processed any tasks and are under our limit.
    while processed:

        processed = _run(taskq_service, queue_names)
        tasks_processed += processed
        iterations += 1

        if max_iterations and iterations >= max_iterations:
            break

    return {'iterations': iterations, 'tasks_processed': tasks_processed}


def get_tasks(taskq_service, queue_names=None):
    """
    Get all tasks from queues and return them in a dict keyed by queue_name.
    If queue_names not specified, returns tasks from all queues.

    :param taskq_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queue_names: :class: `list` of queue name strings.
    """

    # Make sure queue_names is a list
    if isinstance(queue_names, basestring):
        queue_names = [queue_names]

    if not queue_names:
        queue_names = get_queue_names(taskq_service)

    task_dict = defaultdict(list)

    for queue_name in queue_names:
        # Get tasks
        tasks = taskq_service.GetTasks(queue_name)

        task_dict[queue_name].extend(tasks)

    return task_dict


def add_tasks(taskq_service, task_dict):
    """
    Allow readding of multiple tasks across multiple queues.
    The task_dict is a dictionary with tasks for each queue, keyed by queue
    name.
    Tasks themselves can be dicts like those received from GetTasks() or Task
    instances.

    :param taskq_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queue_names: :class: `dict` of queue name: tasks dictionary.
    """

    num_added = 0

    # Get the descriptions so we know when to specify PULL mode.
    queue_descriptions = taskq_service.GetQueues()
    queue_desc_dict = dict((queue_desc['name'], queue_desc)
                           for queue_desc in queue_descriptions)

    # Loop over queues and add tasks for each.
    for queue_name, tasks in task_dict.items():

        queue = taskqueue.Queue(queue_name)

        is_pullqueue = ('pull' == queue_desc_dict[queue_name]['mode'])

        tasks_to_add = []

        # Ensure tasks are formatted to add to queues.
        for task in tasks:

            # If already formatted as a Task, add it.
            if isinstance(task, taskqueue.Task):
                tasks_to_add.append(task)
                continue

            # If in dict format that comes from GetTasks(), format it as a Task
            # First look for payload.  If no payload, look for body to decode.
            if 'payload' in task:
                payload = task['payload']
            else:
                payload = base64.b64decode(task.get('body'))

            # Setup different parameters for pull and push queues
            if is_pullqueue:
                task_obj = taskqueue.Task(payload=payload,
                                          name=task.get('name'),
                                          method='PULL',
                                          url=task.get('url'))
            else:
                task_obj = taskqueue.Task(payload=payload,
                                          name=task.get('name'),
                                          method=task.get('method'))

            tasks_to_add.append(task_obj)

        # Add tasks to queue
        if tasks_to_add:
            queue.add(tasks_to_add)
            num_added += len(tasks_to_add)

    return num_added


def purge_tasks(taskq_service, queue_names=None):
    """
    Remove all tasks from queues.

    :param taskq_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queue_names: :class: `list` of queue name strings.
    """

    # Make sure queue_names is a list
    if isinstance(queue_names, basestring):
        queue_names = [queue_names]

    if not queue_names:
        queue_names = get_queue_names(taskq_service)

    num_tasks = 0

    for queue_name in queue_names:
        # Get tasks to help give some feedback
        tasks = taskq_service.GetTasks(queue_name)
        num_tasks += len(tasks)

        taskq_service.FlushQueue(queue_name)

    return num_tasks


def get_queue_names(taskq_service, mode=None):
    """Returns push queue names from the Task Queue service."""

    queue_descriptions = taskq_service.GetQueues()

    return [description['name']
            for description in queue_descriptions]


def get_pull_queue_names(taskq_service, mode=None):
    """Returns pull queue names from the Task Queue service."""

    queue_descriptions = taskq_service.GetQueues()

    return [description['name']
            for description in queue_descriptions
            if 'pull' == description.get('mode')]


def get_push_queue_names(taskq_service, mode=None):
    """Returns push queue names from the Task Queue service."""

    queue_descriptions = taskq_service.GetQueues()

    return [description['name']
            for description in queue_descriptions
            if 'push' == description.get('mode')]


class Runner(object):
    """A class to help run pull queues.

    Allows parameters such as taskq_service and queue_names be specified at
    __init__ instead of in each run() call.
    """
    # TODO: WRITE UNIT TESTS FOR ME.

    def __init__(self, taskq_service, queue_names=None):
        """Store taskq_service and optionally queue_name list for reuse."""

        self.taskq_service = taskq_service

        if None == queue_names:
            self.queue_names = get_push_queue_names(self.taskq_service)
        else:
            self.queue_names = queue_names

    def run(self, max_iterations=None):
        """Run the existing tasks for all pushqueue."""

        return run(self.taskq_service, self.queue_names, max_iterations)

    def run_queue(self, queue_name):
        """Run all the existing tasks for one queue."""

        return run_queue(self.taskq_service, queue_name)


def _execute_task(task):
    """Extract the body and header from the task and process it."""

    # Ensure each test looks like it is in a new request.
    os.environ['REQUEST_ID_HASH'] = uuid.uuid4().hex

    # Decode the body and process the task.
    body = base64.b64decode(task['body'])
    return_code, func_path = process_async_task(dict(task['headers']), body)

    # TODO: Possibly do more with return_codes.

    # Cleanup context since we will be executing more tasks in this process.
    _clear_context()
    del os.environ['REQUEST_ID_HASH']


def _run(taskq_service, queue_names):
    """Run individual tasks in push queues.

    :param taskq_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queue_names: :class: `list` of queue name strings
    """

    num_processed = 0

    # Process each queue
    # TODO: Round robin instead of one queue at a time.
    for queue_name in queue_names:
        num_processed += run_queue(taskq_service, queue_name)

    return num_processed

def run_random(queue_service, queues, random_seed=123, max_tasks=100):
    """Run individual tasks in push queues.

    :param queue_service: :class: `taskqueue_stub.TaskQueueServiceStub`
    :param queues: :class: `dict` of queue 'descriptions'
    """
    if not queues:
        return 0

    queue_count = len(queues)

    num_processed = 0
    
    random.seed(random_seed)

    while num_processed < max_tasks:

        queue_index = random.randrange(queue_count)
        queue_desc = queues[queue_index]
        processed_queue_count = 0
        
        task_ran = False
        
        while not task_ran and processed_queue_count < queue_count:

            # Only process from push queues.
            if queue_desc.get('mode') == 'push':
                queue_name = queue_desc['name']
                task_ran = _run_random_task_from_queue(queue_service, queue_name)

            if task_ran:
                num_processed += 1
            else:
                queue_index += 1
                queue_desc = queues[queue_index % queue_count]
                processed_queue_count += 1

        if not task_ran:
            break

    return num_processed

def _run_random_task_from_queue(queue_service, queue_name):
    """Attempts to run a random task from the queue identified
    by queue_name.  Returns True if a task was ran otherwise
    returns False.
    """
    task = _fetch_random_task_from_queue(queue_service, queue_name)
    
    if task and task.get('name'):
        _execute_task(task)
        
        queue_service.DeleteTask(queue_name, task.get('name'))
        
        return True
    
    return False
        

def _fetch_random_task_from_queue(queue_service, queue_name):
    """Returns a random task from the queue identified by queue_name
    if there exists at least one task in the queue.
    """
    tasks = queue_service.GetTasks(queue_name)

    if not tasks:
        return None
    
    task = random.choice(tasks)

    if not task:
        return None
    
    return task

### Deprecated ###

def execute_queues(queues, queue_service):
    """ DEPRECATED
    Remove this as soon as references to this in other libraries are gone.
    Use run() or Runner.run() instead of this.

    Run individual tasks in push queues.
    """
    import logging
    logging.warning('This method is deprecated, switch to ')

    num_processed = False

    # Process each queues
    for queue_desc in queues:

        # Don't pull anything from pull queues.
        if queue_desc.get('mode') == 'pull':
            continue

        num_processed = (run_queue(queue_service, queue_desc['name'])
                         or num_processed)

    return bool(num_processed)
