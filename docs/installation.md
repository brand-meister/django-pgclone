# Installation

Install `django-pgclone` with:

    pip3 install django-pgclone

After this, add `pgclone` to the `INSTALLED_APPS` setting of your Django project.

!!! note

    Install the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) to enable the S3 storage backend. Alternatively, install the optional S3 extra (`pip install django-pgclone[s3]`) and set `PGCLONE_S3_BACKEND = "boto3"`.
