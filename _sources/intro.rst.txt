Intro
=====

*mynbou* is Afrikaans for mining, it provides defect prediction dataset extraction from the SmartSHARK database.

It currently contains modules for finding paths in commit graphs, rename tracking and aggregating the metrics over all paths to the files contained in the release.
It implements change metrics as defined by Moser et al. :cite:`moser`, Hassan :cite:`hassan` and the extensions proposed by D'Ambros et al. :cite:`dambros`.

Moreover *mynbou* provides additional aggregations for method level metrics as proposed by Zhang et al. :cite:`zhang`.


Installation
------------


Via PIP
^^^^^^^

.. code-block:: shell-session

    pip install https://github.com/smartshark/mynbou/zipball/master


Via setup.py
^^^^^^^^^^^^

.. code-block:: shell-session
    
    python setup.py install


Run tests
---------

.. code-block:: bash
    
    python setup.py test


.. WARNING:: This software is still in development.


Bibliography
------------

.. bibliography:: references.bib
