# sushi-chef-shls
Sushi Chef script for importing SHLS content from http://shls.rescue.org/

## Setup

### Step 1: Install
* [Install pip](https://pypi.python.org/pypi/pip) if you don't have it already.
* [Install Python3](https://www.python.org/downloads) if you don't have it already.
* [Install Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git) if you don't have it already
* Open a terminal
* Run `git clone https://github.com/learningequality/sushi-chef-shls` 
  then `cd sushi-chef-shls`
* Create a Python3 virtual env `virtualenv -p python3 venv`
  and activate it using `source venv/bin/activate`
* Run `pip install -r requirements.txt`

### Step 2: Obtain a Studio Authorization Token
You will need an authorization token to create a channel on Kolibri Studio.
In order to obtain one:
1. Create an account on [Kolibri Studio](https://studio.learningequality.org/).
2. Navigate to the Tokens tab under your Settings page.
3. Copy the given authorization token (you will need this token to run the sushi chef script).

### Step 3: Obtaining a box.xom API Token
The PDF files of the SHLS toolkit are hosted on box.com file sharing service.
Accessing these files programatically requires using the box.com API, which in
turn requires registering box.com developer, creating an APP, and obtaiing
an API token.
You'll need to place this access token in `credentials/box_com_access_token.txt`


## Running the script

In the root of the chef repo, inside the activated virtual environment, run:
```bash
./sushichef.py --reset --thumbnails --token={studio_token}
```

If everything worked out you'll see a Studio URL of the staged channel appear at
the end of the chef run.